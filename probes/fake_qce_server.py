"""离线 fake QCE 服务器（R2-1/R2-4 修正版）：只谈合成数据，无真实 QQ/网络访问。

与旧版的根本差别（08 复核 P02 §4）：不再按场景硬编码"有界回 5 条 / 默认回 5000 条"。
本服务器从**同一份合成数据集**出发，按请求实际参数计算结果：

  filter.startTime/endTime → 按秒→毫秒规则过滤数据集；
  batchSize                → 每次请求从上游多载入这么多条进"当前已加载范围"（累计、有上界）；
  page / limit             → 在已加载范围内切片；
  totalCount / totalPages  → **只描述当前已加载范围**，不是上游全量；
  hasNext                  → 已加载范围之后上游仍有数据；即缓存末页仍可能扩展（RS01 语义）；
  cacheHit                 → 完全相同的请求体第二次到达；重复体命中缓存时不再扩展。

因此"放大比例"是数据集规模与请求窗口的算术结果，不是硬编码常数；报告必须这样记。

场景名（scenario["fetch"]）：
  数据驱动（默认）: "dataset"
  传输/协议故障   : auth-401 / bad-json / huge / missing-metadata
  业务故障        : business-false-nested / business-false-flat / business-leaky-message
  分页反例        : repeat-same / empty-with-more / page-mismatch / count-contradiction
                    / hasnext-non-bool / string-totalpages / malformed-items
                    / messages-not-array
其它 scenario 键：datasetSize / stepSeconds / delayMs / unboundedReturnsAll(bool)
"""
from __future__ import annotations

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_TOKEN = "SYNTHETIC_TOKEN_NOT_A_SECRET_0123456789abcdef"

# 服务端 message 字段里的诱饵：探针若复制服务端文本就会泄漏（RS04 测试锚点）
LEAKY_SERVER_MESSAGE = (
    "upstream rejected bearer "
    "SYNTHETIC_TOKEN_NOT_A_SECRET_0123456789abcdef from uin 10001"
)

BASE_EPOCH_S = 1_700_000_000
DEFAULT_DATASET_SIZE = 240
DEFAULT_STEP_SECONDS = 60


def build_dataset(count: int = DEFAULT_DATASET_SIZE, step_seconds: int = DEFAULT_STEP_SECONDS,
                  *, base_s: int = BASE_EPOCH_S, same_second_pairs: int = 3) -> list:
    """有序合成数据集：每条秒级 msgTime 递增，另注入同秒不同 ID 的样本（X1 形态）。

    `msgId` 保持 T01 实测的 19 位字符串形态；`msgSeq` 为原始序号（字符串形态，RS06）。
    """
    messages = []
    for i in range(count):
        messages.append({
            "msgId": f"7562417847123{i:06d}",
            "msgSeq": f"{1000 + i}",
            "msgTime": str(base_s + i * step_seconds),
            "senderUin": "10001" if i % 2 == 0 else "20001",
            "sendType": 2 if i % 2 == 0 else 0,
            "msgType": 2,
        })
    for j in range(same_second_pairs):
        anchor = messages[j * 7]
        messages.append({
            "msgId": f"7562417847123{count + j:06d}",
            "msgSeq": f"{1000 + count + j}",
            "msgTime": anchor["msgTime"],  # 同秒第二条，不同原生 ID
            "senderUin": "20001",
            "sendType": 0,
            "msgType": 2,
        })
    messages.sort(key=lambda m: (int(m["msgTime"]), m["msgId"]))
    return messages


def page_data(messages: list, *, page: int, total_pages: int, total_count: int,
              has_next: bool, cache_hit: bool = False) -> dict:
    return {
        "messages": messages,
        "totalCount": total_count,
        "currentPage": page,
        "totalPages": total_pages,
        "hasNext": has_next,
        "cacheHit": cache_hit,
        "fetchedAt": "2026-09-29T00:00:00Z",
    }


def envelope(data: dict) -> bytes:
    return json.dumps({"success": True, "data": data, "requestId": "synthetic-req"}).encode("utf-8")


def business_error_envelope(*, shape: str) -> bytes:
    """QCE 正式错误 envelope 是嵌套 error 对象；同时保留旧的扁平形态做对照。"""
    if shape == "business-false-nested":
        return json.dumps({
            "success": False,
            "error": {"code": "UPSTREAM_TIMEOUT", "message": LEAKY_SERVER_MESSAGE},
            "requestId": "synthetic-req",
        }).encode("utf-8")
    if shape == "business-leaky-message":
        return json.dumps({
            "success": False, "code": "AUTH_HEADER_REJECTED",
            "message": LEAKY_SERVER_MESSAGE, "requestId": "synthetic-req",
        }).encode("utf-8")
    return json.dumps({
        "success": False, "code": "CACHE_MISS_UPSTREAM", "message": "upstream error",
        "requestId": "synthetic-req",
    }).encode("utf-8")


class FakeQCEHandler(BaseHTTPRequestHandler):
    server_version = "FakeQCE/2.0"

    def log_message(self, *args):  # 静默
        pass

    # -- 记录与鉴权 -------------------------------------------------------
    def _record(self, body_bytes: bytes) -> dict:
        entry = {
            "method": self.command,
            "path": self.path,
            "query": self.path.split("?", 1)[1] if "?" in self.path else "",
            "auth": self.headers.get("Authorization"),
            "body": None,
        }
        if body_bytes:
            try:
                entry["body"] = json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                entry["body"] = None
        self.server.requests.append(entry)
        return entry

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.server.expected_token}"

    def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_envelope(self, data: dict) -> None:
        self._send(200, envelope(data))

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._record(b"")
        path = self.path.split("?", 1)[0]
        if self.server.scenario.get("delayMs"):
            time.sleep(self.server.scenario["delayMs"] / 1000)
        if path == "/health":
            self._send(200, b'{"ok": true}')
            return
        if not self._authorized():
            self._send(401, b'{"success": false, "code": 401}')
            return
        if self.server.scenario.get("redirect") and path == "/api/system/info":
            port = self.server.server_address[1]
            self._send_redirect(f"http://127.0.0.1:{port}/elsewhere")
            return
        if path == "/api/system/info":
            # Pinned system.rs:192-199. Never invent a top-level selfUin DTO.
            self.server.identity_checks = getattr(self.server, "identity_checks", 0) + 1
            owner = self.server.scenario.get("selfUin", "10001")
            if self.server.identity_checks > 1 and "selfUinAfter" in self.server.scenario:
                owner = self.server.scenario["selfUinAfter"]
            self._send_envelope({"version": "6.3.0-fake", "mode": "plugin",
                                 "napcat": {"version": "unknown", "online": True,
                                            "selfInfo": {"uin": owner}}})
            return
        if path == "/api/system/status":
            self._send_envelope({"loggedIn": True})
            return
        if path == "/api/friends":
            self._send_envelope({"friends": [{"uin": "20001"}, {"uin": "20002"}], "totalCount": 2})
            return
        if path == "/api/users/lookup":
            self._send_envelope({"peerUid": "u_synthetic_peer"})
            return
        self._send(404, b'{"success": false}')

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body_bytes = self.rfile.read(length) if length else b""
        self._record(body_bytes)
        path = self.path.split("?", 1)[0]
        if path != "/api/messages/fetch":
            self._send(404, b'{"success": false}')
            return
        if not self._authorized():
            self._send(401, b'{"success": false, "code": 401}')
            return
        scenario = self.server.scenario
        name = scenario.get("fetch", "dataset")
        body = self.server.requests[-1]["body"] or {}
        if scenario.get("delayMs"):
            time.sleep(scenario["delayMs"] / 1000)
        if path == "/api/messages/fetch" and scenario.get("arrivals"):
            # CP04 离线证明用：每次 fetch 到达一条预定方向的新消息（时间=当下，落进滑动窗口）
            self.server.deliver_next_arrival()

        # Default fixtures retain the historical friend-only boundary. R4 group
        # fixtures explicitly opt into the pinned protocol's numeric type 2.
        peer = body.get("peer") or {}
        allowed_types=(1,2) if scenario.get('groupEnabled') else (1,)
        if peer.get("chatType") not in allowed_types or type(peer.get("chatType")) is not int:
            self._send_envelope({"code": "INVALID_PEER", "message": "chatType must be 1"})
            return

        if name == "auth-401":
            self._send(401, b'{"success": false, "code": 401}')
            return
        if name in ("business-false-nested", "business-false-flat", "business-leaky-message"):
            self._send(200, business_error_envelope(shape=name))
            return
        if name == "bad-json":
            self._send(200, b"{definitely-not-json")
            return
        if name == "huge":
            self._send(200, b"x" * (5 * 1024 * 1024))
            return
        if name == "missing-metadata":
            self._send(200, json.dumps({
                "success": True, "requestId": "r",
                "data": {"messages": self.server.matched(body)[:2],
                         "totalCount": 2, "currentPage": body.get("page", 1)},
            }).encode())
            return
        if name == "malformed-items":
            good = self.server.matched(body)[:2]
            page = body.get("page", 1)
            items = list(good) + [7, {"noId": True}, {"msgId": 12345}, {"msgId": "   "}, "str"]
            self._send_envelope(page_data(items, page=page, total_pages=page,
                                          total_count=len(items), has_next=False))
            return
        if name == "over-limit":
            page = body.get("page", 1)
            self._send_envelope(page_data(self.server.matched(body)[:2], page=page,
                                          total_pages=page, total_count=2, has_next=False))
            return
        if name == "repeat-same":
            # 页码在前进、缓存计数在增长，但内容始终是同一批 ID：无进展
            page = body.get("page", 1)
            first = self.server.matched(body)[:3]
            self._send_envelope(page_data(first, page=page, total_pages=page,
                                          total_count=6, has_next=True))
            return
        if name == "empty-with-more":
            page = body.get("page", 1)
            self._send_envelope(page_data([], page=page, total_pages=page,
                                          total_count=5, has_next=True))
            return
        if name == "page-mismatch":
            self._send_envelope(page_data(self.server.matched(body)[:3], page=99,
                                          total_pages=99, total_count=3, has_next=False))
            return
        if name == "count-contradiction":
            page = body.get("page", 1)
            self._send_envelope(page_data(self.server.matched(body)[:3], page=page,
                                          total_pages=page, total_count=1, has_next=False))
            return
        if name == "hasnext-non-bool":
            page = body.get("page", 1)
            data = page_data(self.server.matched(body)[:3], page=page, total_pages=page,
                             total_count=3, has_next=False)
            data["hasNext"] = "false"
            self._send_envelope(data)
            return
        if name == "string-totalpages":
            page = body.get("page", 1)
            data = page_data(self.server.matched(body)[:3], page=page, total_pages=page,
                             total_count=3, has_next=False)
            data["totalPages"] = str(page)
            self._send_envelope(data)
            return
        if name == "messages-not-array":
            page = body.get("page", 1)
            self._send(200, json.dumps({
                "success": True, "requestId": "r",
                "data": {"messages": {"oops": True}, "totalCount": 3, "currentPage": page,
                         "totalPages": page, "hasNext": False},
            }).encode())
            return

        self._serve_dataset(body)

    # -- 数据驱动引擎 -----------------------------------------------------
    def _serve_dataset(self, body: dict) -> None:
        server = self.server
        page = body.get("page") or 1
        limit = body.get("limit") or 50
        batch = body.get("batchSize")
        window = body.get("filter")
        unbounded = window is None or batch is None

        if unbounded:
            # 默认路径：不带 filter/batchSize 时一次返回整个上游。条数由数据集规模决定，
            # 不是硬编码常数——放大比例因此是可核对的算术结果（R2-4/P02）。
            all_rows = server.dataset
            self._send_envelope(page_data(
                list(all_rows), page=1, total_pages=max(1, math.ceil(len(all_rows) / limit)),
                total_count=len(all_rows), has_next=False))
            return

        key = json.dumps(body, sort_keys=True)
        replay = server.cached_responses.get(key)
        if replay is not None:
            replay = dict(replay)
            replay["cacheHit"] = True
            self._send_envelope(replay)
            return

        matched = server.matched(body)
        scope_key = (body.get("peer", {}).get("peerUid"), tuple(sorted(window.items())))
        prev = server.cache_loaded.get(scope_key, 0)
        loaded = min(len(matched), prev + int(batch))
        server.cache_loaded[scope_key] = loaded
        # 只允许读到"当前已加载范围"内的行；totalCount/totalPages 同样只描述这一段
        start = (int(page) - 1) * int(limit)
        rows = matched[:loaded][start:start + int(limit)]
        payload = page_data(
            rows, page=int(page),
            total_pages=max(1, math.ceil(loaded / int(limit))),
            total_count=loaded,
            has_next=loaded < len(matched),
            cache_hit=False,
        )
        server.cached_responses[key] = payload
        self._send_envelope(payload)


def filter_rows(rows: list, start_ms=None, end_ms=None) -> list:
    """秒→毫秒只在这一处转换；窗口取 start <= t < end。fake 与实验共用同一实现。"""
    out = rows
    if start_ms is not None:
        out = [m for m in out if int(m["msgTime"]) * 1000 >= int(start_ms)]
    if end_ms is not None:
        out = [m for m in out if int(m["msgTime"]) * 1000 < int(end_ms)]
    return out


class FakeQCE(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """探针在 deadline 测试里会主动掐断连接：这是预期行为，不打印整段 traceback。"""
        self.aborted_connections = getattr(self, "aborted_connections", 0) + 1

    def setup_scenario(self, scenario: dict, token: str = DEFAULT_TOKEN) -> None:
        self.scenario = scenario
        self.expected_token = token
        self.requests: list[dict] = []
        self.aborted_connections = 0
        self.cached_responses: dict[str, dict] = {}
        self.cache_loaded: dict[tuple, int] = {}
        self.dataset = build_dataset(
            scenario.get("datasetSize", DEFAULT_DATASET_SIZE),
            scenario.get("stepSeconds", DEFAULT_STEP_SECONDS),
            base_s=scenario.get("baseEpochS", BASE_EPOCH_S),
            same_second_pairs=scenario.get("sameSecondPairs", 3),
        )
        self.arrivals = list(scenario.get("arrivals", []))
        self.arrived = 0

    def deliver_next_arrival(self) -> None:
        """把一条预定的"新消息"放进数据集，用于离线证明 CP04 能看见新增并判定方向。"""
        if not self.arrivals:
            return
        direction = self.arrivals.pop(0)
        self.arrived += 1
        now_s = int(time.time()) - 1  # 退一秒，避免与滑动窗口 end 落在同一秒而被边界排除
        self.dataset.append({
            "msgId": f"756241784799{self.arrived:08d}",
            "msgSeq": f"{9000 + self.arrived}",
            "msgTime": str(now_s),
            "senderUin": "10001" if direction == "self" else "20001",
            "sendType": 2 if direction == "self" else 0,
            "msgType": 2,
        })
        self.dataset.sort(key=lambda m: (int(m["msgTime"]), m["msgId"]))
        # 新行进入上游后，此前"已读完"的缓存范围不再等价，清空以便重新扩展
        self.cache_loaded.clear()

    def matched(self, body: dict) -> list:
        """按请求 filter 的毫秒窗口过滤数据集（与实验断言共用 filter_rows）。"""
        window = body.get("filter") or {}
        return filter_rows(self.dataset, window.get("startTime"), window.get("endTime"))


class RunningFakeQCE:
    """可跨多次探针调用共存的 fake server（缓存命中/多配置实验需要）。"""

    def __init__(self, scenario: dict):
        self.server = FakeQCE(("127.0.0.1", 0), FakeQCEHandler)
        self.server.setup_scenario(scenario)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningFakeQCE":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def requests(self) -> list:
        return self.server.requests

    @property
    def dataset_size(self) -> int:
        return len(self.server.dataset)

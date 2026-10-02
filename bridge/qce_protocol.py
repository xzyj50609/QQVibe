"""Shared, bounded QCE read protocol for production and offline probes. No CLI or file access."""
from __future__ import annotations

import collections
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client

USER_AGENT = "QQVibe-R2-probe/0.3 (read-only, loopback-only)"


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


CHAT_TYPE_FRIEND = 1  # T01 §8.4：QCE 6.3.0 parse_peer 要求数字，1=私聊


DEFAULT_BATCH_SIZE = 200


MAX_BATCH_SIZE = 5000  # messages.rs clamp 1..5000


DEFAULT_PAGE_SIZE = 50


DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


REQUIRED_PAGE_FIELDS = ("messages", "totalCount", "currentPage", "totalPages", "hasNext")


ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class ProbeFailure(Exception):
    """阶段化失败：stage ∈ config/auth/http/business/protocol。"""

    def __init__(self, stage: str, detail: str):
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


class BudgetExhausted(Exception):
    """预算到顶：不是失败，而是"本窗口未扫完"，必须落为 partial。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 让 urllib 抛 3xx HTTPError，由上层按协议错误处理


class _DeadlineSocket:
    """Keep absolute time/cancellation checks inside each packet read, including headers."""
    def __init__(self, sock, budget, check):
        self.sock, self.budget, self.check = sock, budget, check

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def recv_into(self, *args):
        if self.check is not None:
            self.check()
        self.sock.settimeout(self.budget.timeout_for_request())
        result = self.sock.recv_into(*args)
        self.budget.recheck_after_response()
        return result

    def makefile(self, *args, **kwargs):
        stream = self.sock.makefile(*args, **kwargs)
        raw = getattr(stream, "raw", None)
        if raw is None or not hasattr(raw, "_sock"):
            stream.close()
            raise ProbeFailure("protocol", "bounded socket reader unavailable")
        raw._sock = self
        return stream


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, budget, check, **kwargs):
        self._budget, self._check = budget, check
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        self.sock = _DeadlineSocket(self.sock, self._budget, self._check)


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, budget, check, **kwargs):
        self._budget, self._check = budget, check
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        self.sock = _DeadlineSocket(self.sock, self._budget, self._check)


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(lambda *args, **kwargs: _DeadlineHTTPConnection(*args,
            budget=request._qq_budget, check=request._qq_check, **kwargs), request)


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(lambda *args, **kwargs: _DeadlineHTTPSConnection(*args,
            budget=request._qq_budget, check=request._qq_check, **kwargs), request,
            context=self._context)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect,
                                    _DeadlineHTTPHandler(), _DeadlineHTTPSHandler())


def positive_int(name: str, value, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProbeFailure("config", f"{name} must be an integer")
    upper = maximum if maximum is not None else 2**63 - 1
    if value < 1 or value > upper:
        raise ProbeFailure("config", f"{name} must be within 1..{upper}")
    return value


def positive_finite(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeFailure("config", f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ProbeFailure("config", f"{name} must be finite and greater than zero")
    return number


class Budget:
    """统一预算：进程内每一次 HTTP 请求都从这里过账（RS03）。

    `max_total_requests` 覆盖 GET 检查 + fetch + 任何附加请求；
    `max_fetch_requests` 是 fetch 专用子预算，在报告中单独命名，不冒充总上限。
    """

    def __init__(self, *, max_total_requests: int, max_fetch_requests: int,
                 max_pages: int, max_messages: int, max_seconds: float,
                 request_timeout: float, parent=None):
        self.max_total_requests = max_total_requests
        self.max_fetch_requests = max_fetch_requests
        self.max_pages = max_pages
        self.max_messages = max_messages
        self.max_seconds = max_seconds
        self.request_timeout = request_timeout
        self.started_at = time.monotonic()
        self.used_total = 0
        self.used_fetch = 0
        self.pages_recorded = 0
        self.ids_accepted = 0
        self.parent = parent

    def start(self) -> None:
        self.started_at = time.monotonic()

    def time_left(self) -> float:
        return self.started_at + self.max_seconds - time.monotonic()

    def timeout_for_request(self) -> float:
        """单请求 socket 超时受剩余总时长约束，杜绝"0.03 秒预算配 30 秒超时"。"""
        left = self.time_left()
        if left <= 0:
            raise BudgetExhausted("max-seconds budget reached")
        return min(self.request_timeout, left,
                   self.parent.timeout_for_request() if self.parent is not None else float("inf"))

    def charge(self, kind: str) -> None:
        if self.used_total >= self.max_total_requests:
            raise BudgetExhausted("max-total-requests budget reached")
        if kind == "fetch" and self.used_fetch >= self.max_fetch_requests:
            raise BudgetExhausted("max-fetch-requests budget reached")
        if self.parent is not None:
            self.parent.charge(kind)
        self.used_total += 1
        if kind == "fetch":
            self.used_fetch += 1

    def recheck_after_response(self) -> None:
        """响应返回后复查：慢请求不得让实际耗时越过申报的上限。"""
        if self.time_left() <= 0:
            raise BudgetExhausted("max-seconds exceeded during request")
        if self.parent is not None:
            self.parent.recheck_after_response()

    def fetch_headroom(self) -> str | None:
        """fetch 循环的页级/条级前置检查；返回耗尽原因或 None。"""
        if self.pages_recorded >= self.max_pages:
            return "max-pages budget reached"
        if self.used_fetch >= self.max_fetch_requests:
            return "max-fetch-requests budget reached"
        if self.used_total >= self.max_total_requests:
            return "max-total-requests budget reached"
        if self.ids_accepted >= self.max_messages:
            return "max-messages budget reached"
        if self.time_left() <= 0:
            return "max-seconds budget reached"
        if self.parent is not None:
            return self.parent.fetch_headroom()
        return None

    def snapshot(self) -> dict:
        return {
            "maxTotalRequests": self.max_total_requests,
            "maxFetchRequests": self.max_fetch_requests,
            "maxPages": self.max_pages,
            "maxMessages": self.max_messages,
            "maxSeconds": self.max_seconds,
            "requestTimeout": self.request_timeout,
            "usedTotalRequests": self.used_total,
            "usedFetchRequests": self.used_fetch,
            "pagesRecorded": self.pages_recorded,
            "idsAccepted": self.ids_accepted,
            "elapsedMs": int((time.monotonic() - self.started_at) * 1000),
        }


def validate_base(raw) -> str:
    """只接受裸的本机回环 origin：无 userinfo、无路径、无 query、无 fragment。

    返回值由已校验的 (scheme, host, port) 重新拼装，被拒输入原文绝不外流。
    """
    if not isinstance(raw, str) or not raw or raw.strip() != raw or any(c.isspace() for c in raw):
        raise ProbeFailure("config", "base must be a single non-empty token without whitespace")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port  # 越界/非法端口在这里抛 ValueError
    except ValueError:
        raise ProbeFailure("config", "base is not a valid URL") from None
    if parsed.scheme not in ("http", "https"):
        raise ProbeFailure("config", "base scheme must be http(s)")
    if parsed.username is not None or parsed.password is not None:
        raise ProbeFailure("config", "base must not contain credentials")
    if parsed.query:
        raise ProbeFailure("config", "base must not contain a query string")
    if parsed.fragment:
        raise ProbeFailure("config", "base must not contain a fragment")
    if parsed.path.rstrip("/"):
        raise ProbeFailure("config", "base must not contain a path")
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ProbeFailure(
            "config", "refusing non-loopback target; this probe only talks to the local QCE"
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme}://{netloc}"


class Client:
    """带预算过账、尺寸上限、禁重定向、Bearer-only 的 JSON 客户端。"""

    def __init__(self, base: str, token: str, budget: Budget, *, max_response_bytes: int):
        self.base = base
        self.token = token
        self.budget = budget
        self.max_response_bytes = max_response_bytes

    def request(self, kind: str, method: str, path: str, label: str, *, body: dict | None = None):
        """返回 (status, payload, elapsed_ms)。`label` 不含凭证，用于所有错误文本。"""
        timeout = self.budget.timeout_for_request()
        deadline_clamped = timeout < self.budget.request_timeout
        self.budget.charge(kind)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        req._qq_budget = self.budget
        req._qq_check = getattr(self, "check", None)
        started = time.monotonic()
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                status = resp.status
                chunks, size = [], 0
                while size <= self.max_response_bytes:
                    check = getattr(self, "check", None)
                    if check is not None:
                        check()
                    current_timeout = self.budget.timeout_for_request()
                    deadline_clamped = current_timeout < self.budget.request_timeout
                    if resp.fp is None:
                        break
                    transport_socket = getattr(getattr(resp.fp, "raw", None), "_sock", None)
                    if transport_socket is None or not callable(getattr(resp, "read1", None)):
                        raise ProbeFailure("protocol", f"{label}: bounded response transport unavailable")
                    transport_socket.settimeout(current_timeout)
                    part = resp.read1(min(65536, self.max_response_bytes + 1 - size))
                    self.budget.recheck_after_response()
                    if not part:
                        break
                    chunks.append(part)
                    size += len(part)
                raw = b"".join(chunks)
        except urllib.error.HTTPError as exc:
            exc.close()
            if 300 <= exc.code < 400:
                raise ProbeFailure("protocol", f"{label}: redirect refused (HTTP {exc.code})") from None
            if exc.code in (401, 403):
                raise ProbeFailure(
                    "auth", f"{label}: HTTP {exc.code}; token invalid or missing — not retrying"
                ) from None
            raise ProbeFailure("http", f"{label}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if (deadline_clamped and (isinstance(exc, TimeoutError)
                                      or isinstance(getattr(exc, "reason", None), TimeoutError))):
                # 单请求超时是被 max-seconds 收紧后的结果，属于"未扫完"而非上游故障：落 partial
                raise BudgetExhausted("max-seconds exceeded during request") from None
            raise ProbeFailure("http", f"{label}: transport error ({type(exc).__name__})") from None
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.budget.recheck_after_response()
        if len(raw) > self.max_response_bytes:
            raise ProbeFailure(
                "protocol",
                f"{label}: response exceeds max-response-bytes={self.max_response_bytes} "
                f"(read {len(raw)} bytes, stream truncated)",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProbeFailure(
                "protocol", f"{label}: non-JSON response ({type(exc).__name__})"
            ) from None
        self.budget.recheck_after_response()
        return status, payload, elapsed_ms


def validate_error_code(value) -> str:
    """只接受经校验的错误码标识，绝不复制服务端任意 message 文本（RS04）。"""
    if isinstance(value, str) and ERROR_CODE_RE.match(value):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "unclassified"


def unwrap_envelope(payload, label: str) -> tuple[dict, object]:
    """RV02：/api/* 必须是 {success:true, data:{...}, requestId?}；否则失败而非空消息。"""
    if not isinstance(payload, dict):
        raise ProbeFailure("protocol", f"{label}: expected JSON object, got {type(payload).__name__}")
    if "success" not in payload:
        keys = ",".join(sorted(payload.keys())[:8])
        raise ProbeFailure("protocol", f"{label}: envelope missing 'success' (keys={keys})")
    if payload["success"] is not True:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else payload.get("code")
        raise ProbeFailure("business", f"{label}: success=false code={validate_error_code(code)}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProbeFailure("protocol", f"{label}: 'data' is {type(data).__name__}, expected object")
    return data, payload.get("requestId")


def require_int(value, label: str, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProbeFailure(
            "protocol", f"{label}: {name} must be a non-boolean integer, got {type(value).__name__}"
        )
    if value < minimum:
        raise ProbeFailure("protocol", f"{label}: {name} must be >= {minimum}, got {value}")
    return value


def parse_page_envelope(data: dict, *, requested_page: int, label: str) -> dict:
    """校验分页元数据；缺字段与结构矛盾分开处理（RS02）。

    返回 `{"missing_fields": [...]}` 表示缺元数据（partial），
    抛 ProbeFailure 表示结构矛盾（protocol 失败），
    否则返回规范化字段。
    """
    missing = [k for k in REQUIRED_PAGE_FIELDS if k not in data]
    if missing:
        return {"missing_fields": missing}

    current = require_int(data["currentPage"], label, name="currentPage", minimum=1)
    total_pages = require_int(data["totalPages"], label, name="totalPages", minimum=0)
    total_count = require_int(data["totalCount"], label, name="totalCount", minimum=0)
    has_next = data["hasNext"]
    if not isinstance(has_next, bool):
        raise ProbeFailure(
            "protocol", f"{label}: hasNext must be a boolean, got {type(has_next).__name__}"
        )
    messages = data["messages"]
    if not isinstance(messages, list):
        raise ProbeFailure(
            "protocol", f"{label}: messages must be an array, got {type(messages).__name__}"
        )

    # 页码须与请求一致；以下矛盾说明响应不属于本次请求，不能继续当覆盖证据
    if current != requested_page:
        raise ProbeFailure(
            "protocol", f"{label}: currentPage={current} but page={requested_page} was requested"
        )
    if total_pages and current > total_pages:
        raise ProbeFailure("protocol", f"{label}: currentPage={current} exceeds totalPages={total_pages}")
    if len(messages) > total_count:
        raise ProbeFailure(
            "protocol",
            f"{label}: returnedCount={len(messages)} exceeds totalCount={total_count}",
        )
    if not messages and total_count > 0 and not has_next:
        raise ProbeFailure(
            "protocol", f"{label}: empty page with totalCount={total_count} and hasNext=false"
        )

    ids, invalid = validate_message_items(messages)
    return {
        "missing_fields": [],
        "current": current,
        "total_pages": total_pages,
        "total_count": total_count,
        "has_next": has_next,
        "messages": messages,
        "ids": ids,
        "invalid": invalid,
    }


def validate_message_items(messages: list) -> tuple[list[str], dict]:
    """每项必须是对象且带非空字符串 msgId；坏项计数而非静默丢弃（RS02）。"""
    ids: list[str] = []
    kinds: collections.Counter = collections.Counter()
    for item in messages:
        if not isinstance(item, dict):
            kinds[f"not-object:{type(item).__name__}"] += 1
            continue
        if "msgId" not in item:
            kinds["msgId-absent"] += 1
            continue
        raw_id = item["msgId"]
        if not isinstance(raw_id, str):
            kinds[f"msgId-type:{type(raw_id).__name__}"] += 1
            continue
        if not raw_id.strip():
            kinds["msgId-empty"] += 1
            continue
        ids.append(raw_id)
    return ids, {"count": len(messages) - len(ids), "kinds": dict(kinds)}


def value_shape(value) -> dict:
    """ID/时间戳只记形态，不记原值。"""
    if isinstance(value, str):
        return {"type": "str", "length": len(value)}
    if isinstance(value, bool):
        return {"type": "bool"}
    if isinstance(value, int):
        return {"type": "int", "digits": len(str(abs(value)))}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def summarize_messages(messages: list) -> dict:
    """结构摘要：数量、类型分布、ID/时间形态、方向字段出现情况。不含正文与原值。"""
    type_counter = collections.Counter(
        str(m.get("msgType", m.get("type", "?"))) for m in messages if isinstance(m, dict)
    )
    sender_fields: collections.Counter = collections.Counter()
    send_type_counter: collections.Counter = collections.Counter()
    id_shapes: dict[str, dict] = {}
    time_shapes: dict[str, dict] = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        sender_fields.update(k for k in ("senderUid", "senderUin") if k in m)
        if "sendType" in m:
            send_type_counter[str(m["sendType"])] += 1
        for key in ("msgId", "msgSeq", "realId"):
            if key in m and key not in id_shapes:
                id_shapes[key] = value_shape(m[key])
        if "msgTime" in m and "msgTime" not in time_shapes:
            time_shapes["msgTime"] = value_shape(m["msgTime"])
    return {
        "returnedCount": len(messages),
        "msgTypeDistribution": dict(type_counter),
        "senderFieldsSeen": dict(sender_fields),
        "sendTypeDistribution": dict(send_type_counter),
        "idShapes": id_shapes,
        "timeShapes": time_shapes,
    }


def build_fetch_body(peer_uid: str, page: int, *, batch_size: int, page_size: int,
                     window: tuple[int, int] | None, chat_type: int = CHAT_TYPE_FRIEND) -> dict:
    if type(chat_type) is not int or chat_type not in (1,2):
        raise ValueError('invalid-chat-type')
    body: dict = {
        "peer": {"chatType": chat_type, "peerUid": peer_uid},
        "page": page,
        "limit": page_size,
    }
    if batch_size:
        body["batchSize"] = batch_size
    if window is not None:
        body["filter"] = {"startTime": window[0], "endTime": window[1]}
    return body


def bounded_fetch(client: Client, args, window: tuple[int, int], *, on_rows=None, on_page=None) -> dict:
    """固定窗口内的有界扫描。complete 只在合法终态给出（RS01）。"""
    budget = client.budget
    pages: list[dict] = []
    seen_ids: set[str] = set()
    server_reported_total = None
    cache_hits = 0
    invalid_items_total = 0
    status = "complete"
    partial_reason = None
    page = 1
    page_signatures: set[tuple] = set()
    # QCE computes offset=(page-1)*limit. Shrinking limit mid-scan revisits rows.
    page_size = min(args.limit, budget.max_messages - budget.ids_accepted)
    batch_size = min(args.batch_size, budget.max_messages - budget.ids_accepted)

    while True:
        headroom = budget.fetch_headroom()
        if headroom:
            status, partial_reason = "partial", headroom
            break

        capacity = budget.max_messages - budget.ids_accepted
        if capacity < page_size:
            status, partial_reason = "partial", "max-messages budget cannot fit the fixed next page"
            break
        body = build_fetch_body(
            args.peer_uid, page, batch_size=batch_size,
            page_size=page_size, window=window, chat_type=getattr(args,'chat_type',CHAT_TYPE_FRIEND),
        )
        label = f"messages/fetch page={page}"
        try:
            _, payload, elapsed = client.request(
                "fetch", "POST", "/api/messages/fetch", label, body=body
            )
        except BudgetExhausted as exc:
            status, partial_reason = "partial", exc.reason
            break

        data, request_id = unwrap_envelope(payload, label)
        parsed = parse_page_envelope(data, requested_page=page, label=label)

        record = {
            "requestedPage": page,
            "requestedLimit": page_size,
            "elapsedMs": elapsed,
            "requestId": request_id,
            "cacheHit": data.get("cacheHit"),
            "fetchedAt": data.get("fetchedAt"),
        }
        if data.get("cacheHit") is True:
            cache_hits += 1
        budget.pages_recorded += 1

        if parsed["missing_fields"]:
            record["missingFields"] = parsed["missing_fields"]
            pages.append(record)
            status = "partial"
            partial_reason = f"missing pagination metadata: {parsed['missing_fields']}"
            break

        record.update({
            "currentPage": parsed["current"],
            "totalPages": parsed["total_pages"],
            "totalCount": parsed["total_count"],
            "hasNext": parsed["has_next"],
            "returnedCount": len(parsed["messages"]),
        })
        record["messagesSummary"] = summarize_messages(parsed["messages"])
        pages.append(record)
        if server_reported_total is None:
            server_reported_total = parsed["total_count"]

        # 首次出现顺序求集合差，保证 newIds 可复现（RS02）
        new_ids = [i for i in dict.fromkeys(parsed["ids"]) if i not in seen_ids]
        accepted = new_ids[:max(0, capacity)]
        not_counted = len(new_ids) - len(accepted)
        record["newIds"] = len(accepted)
        seen_ids.update(accepted)
        budget.ids_accepted += len(accepted)
        if on_rows is not None:
            selected = set(accepted)
            validated_rows = []
            for item in parsed["messages"]:
                native_id = item.get("msgId") if isinstance(item, dict) else None
                if isinstance(native_id, str) and native_id in selected:
                    validated_rows.append(item)
                    selected.remove(native_id)
            on_rows(validated_rows, record)
        # Production needs every observed version of a native ID, including a
        # competing body later in the window. Measurement's on_rows stays unique.
        if on_page is not None and len(parsed["messages"]) <= page_size:
            on_page(parsed["messages"], record)

        if parsed["invalid"]["count"]:
            invalid_items_total += parsed["invalid"]["count"]
            record["invalidItems"] = parsed["invalid"]
            status = "partial"
            partial_reason = (
                f"{parsed['invalid']['count']} malformed message item(s): "
                f"{sorted(parsed['invalid']['kinds'])}"
            )
            break

        if not parsed["messages"] and parsed["has_next"]:
            status, partial_reason = "partial", "empty page while hasNext=true (no progress)"
            break
        if not parsed["messages"] and parsed["total_count"] == 0:
            status = "complete-empty"
            break

        if not new_ids:
            status, partial_reason = "partial", "duplicate page (no new msgId)"
            break

        if not_counted:
            record["returnedNotCounted"] = not_counted
            status, partial_reason = "partial", "max-messages budget reached mid-response"
            break
        if len(parsed["messages"]) > page_size:
            status, partial_reason = "partial", "response exceeds the fixed requested page size"
            break

        signature = (parsed["current"], parsed["total_pages"], parsed["total_count"],
                     tuple(accepted))
        if signature in page_signatures:
            status, partial_reason = "partial", "repeated page signature without progress"
            break
        page_signatures.add(signature)

        at_cache_end = parsed["current"] >= parsed["total_pages"]
        if not parsed["has_next"]:
            if at_cache_end:
                status = "complete"  # 缓存读完 **且** 服务端声明无更多：合法终态
                break
            page += 1  # 缓存里还有已加载页，先读完（RS01 反例二）
            continue

        # hasNext=true：totalPages 只描述当前已加载范围，下一页可能扩展（RS01 反例一）
        page += 1

    return {
        "window": {"startMs": window[0], "endMs": window[1], "unit": "epoch-milliseconds"},
        "batchSize": args.batch_size,
        "pageSize": args.limit,
        "budget": budget.snapshot(),
        "requestsUsed": budget.used_fetch,
        "pages": pages,
        "uniqueMessageIds": len(seen_ids),
        "serverReportedTotalFirstPage": server_reported_total,
        "cacheHits": cache_hits,
        "invalidMessageItems": invalid_items_total,
        "status": status,
        "partialReason": partial_reason,
    }

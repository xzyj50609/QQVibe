"""QCE 本机接口只读探查脚本（R2-1/R2-2 修正版）。

仅调用已从固定源码（QCE 6.3.0 实装 / 参考 7fcca888）与 T01 实测确认的只读端点：
  GET  /health                        （免鉴权，原始 JSON）
  GET  /api/system/info               （业务 envelope）
  GET  /api/system/status             （业务 envelope）
  GET  /api/friends?page=1&limit=50   （业务 envelope）
  GET  /api/users/lookup?uin=<uin>    （仅 --uin 时）
  POST /api/messages/fetch            （业务 envelope；仅单聊）

R2 修正（08 复核 RS01–RS04）：

RS01 缓存末页 ≠ 读完上游。`currentPage >= totalPages` 只在 `hasNext is False` 时才是合法
  终态；`hasNext is True` 时在同一固定窗口和剩余预算内继续请求扩展。无进展（零新 ID、
  重复页签名、空页却声称更多）一律 partial，绝不 complete。
RS02 逐页结构校验：`messages` 必须是数组，每项必须是对象且带非空字符串 `msgId`；坏项计数
  并终止为 partial，不再混在好消息中被忽略。`currentPage/totalPages/totalCount` 必须是
  非 bool 整数且满足约束，`hasNext` 必须是 bool，`currentPage` 必须等于请求页码；结构矛盾
  按 protocol 失败处理。`newIds` 按首次出现顺序求集合差。合法空、缺字段、结构矛盾分别处理。
RS03 预算即执行上限：统一 `Budget` 计数**所有** HTTP 请求（GET 检查、fetch、任何附加请求）。
  `--max-total-requests` 是进程总上限，`--max-fetch-requests` 是 fetch 专用子预算，报告分列。
  扫描开始时选定 `limit`，之后保持不变；剩余额度不足整页时提前 partial。越界响应
  记 `returnedNotCounted` 并判 partial。deadline 同时
  收紧单请求 socket 超时，并在响应返回后复查。所有预算参数须为有限正数。
  `--compare-default` 已从本探针 CLI 移除：默认负载模型对照只存在于离线测试入口。
RS04 凭证不回显：argparse 错误经 `SafeArgumentParser` 抑制原文；`--base` 只接受裸回环 origin
  （拒绝 userinfo/路径/query/fragment，且重建 netloc 不回传原始输入）；错误信息按标签引用
  端点而不拼完整 URL；业务错误只提取经校验的错误码，不复制服务端 message；令牌文件读取失败
  只报脱敏 config 错误。stdout/stderr/SUMMARY_JSON/异常栈四条出口统一收口。

安全边界：
  - 不导出、不写文件、不调用 roaming/导出类端点；
  - 输出只含结构信息（数量、字段名、ID/时间戳形态、类型分布），不打印消息正文与 ID 原值；
  - 令牌不落盘、不回显；--token-file 由用户自行保证文件权限。

用法（Windows PowerShell，令牌不进命令行历史也不回显）：
  $secureToken = Read-Host "QCE token" -AsSecureString
  $env:QCE_TOKEN = [System.Net.NetworkCredential]::new('', $secureToken).Password
  python probes/qce_probe.py --base http://127.0.0.1:40653 --peer-uid <uid> `
      --window-minutes 10 --batch-size 200 --limit 50 --max-pages 3

退出码：0=各阶段按预算完成（fetch 仍可能 partial，看 fetch.status/partialReason）；
2=存在失败阶段（auth/http/business/protocol/config/unexpected）。
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from qce_protocol import (
    Budget, BudgetExhausted, CHAT_TYPE_FRIEND, Client, DEFAULT_BATCH_SIZE, DEFAULT_MAX_RESPONSE_BYTES, DEFAULT_PAGE_SIZE, ERROR_CODE_RE, LOOPBACK_HOSTS, MAX_BATCH_SIZE, ProbeFailure, REQUIRED_PAGE_FIELDS, USER_AGENT, _OPENER, _RefuseRedirect, bounded_fetch, build_fetch_body, parse_page_envelope, positive_finite, positive_int, require_int, summarize_messages, unwrap_envelope, validate_base, validate_error_code, validate_message_items, value_shape
)




# 只接受有界的可打印错误码标识，绝不接受任意服务端文本（RS04）

ARGC_ERROR_TEXT = (
    "qce_probe: error: command line could not be parsed (argument text suppressed: "
    "a rejected option may carry a credential)\n"
)






class SafeArgumentParser(argparse.ArgumentParser):
    """argparse 默认把被拒参数原文写进 stderr；调用方可能误传令牌，故抑制原文。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(ARGC_ERROR_TEXT)
        raise SystemExit(2)






# --------------------------------------------------------------------------
# 参数合法性与统一预算
# --------------------------------------------------------------------------






# --------------------------------------------------------------------------
# 凭证与目标
# --------------------------------------------------------------------------


def load_token(token_file: str) -> tuple[str, str]:
    """返回 (token, source)；命令行明文令牌刻意不支持（RV05/RS04）。"""
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as exc:
            # 不拼路径：文件名的写法本身可能带敏感信息
            raise ProbeFailure(
                "config", f"token file could not be read ({type(exc).__name__})"
            ) from None
        if not token:
            raise ProbeFailure("config", "token file is empty")
        return token, "file"
    token = os.environ.get("QCE_TOKEN", "").strip()
    if token:
        return token, "env"
    raise ProbeFailure(
        "config",
        "no token: set QCE_TOKEN env or --token-file; command-line token is intentionally unsupported",
    )








# --------------------------------------------------------------------------
# 分页与逐条结构校验（RS01/RS02）
# --------------------------------------------------------------------------










# --------------------------------------------------------------------------
# 阶段
# --------------------------------------------------------------------------
def run_get_checks(client: Client, *, uin: str) -> tuple[list, str | None]:
    checks: list[tuple[str, bool, str]] = [
        ("/health", False, "health"),
        ("/api/system/info", True, "system/info"),
        ("/api/system/status", True, "system/status"),
        ("/api/friends?page=1&limit=50", True, "friends"),
    ]
    if uin:
        checks.append((f"/api/users/lookup?uin={urllib.parse.quote(uin)}", True, "users/lookup"))
    results: list[dict] = []
    truncated_by = None
    for path, enveloped, label in checks:
        try:
            _, payload, elapsed = client.request("get", "GET", path, f"GET {label}")
        except BudgetExhausted as exc:
            truncated_by = f"{exc.reason} (before GET {label})"
            break
        if enveloped:
            data, _ = unwrap_envelope(payload, f"GET {label}")
            keys = sorted(data.keys())[:12]
        elif isinstance(payload, dict):
            keys = sorted(payload.keys())[:12]
        else:
            keys = [f"<{type(payload).__name__}>"]
        # 只记路径，不记 query：query 含用户输入的 uin
        results.append({"path": path.split("?")[0], "status": "ok", "dataKeys": keys, "elapsedMs": elapsed})
    return results, truncated_by






def resolve_window(args) -> tuple[int, int]:
    if args.window_minutes is not None:
        if args.window_start_ms or args.window_end_ms:
            raise ProbeFailure("config", "use either --window-minutes or explicit ms window, not both")
        positive_finite("window-minutes", args.window_minutes)
        end_ms = int(time.time() * 1000)  # 一次性固定，之后不再漂移
        return end_ms - int(args.window_minutes * 60_000), end_ms
    if not args.window_start_ms or not args.window_end_ms:
        raise ProbeFailure(
            "config",
            "bounded read requires a fixed window: --window-start-ms/--window-end-ms (epoch ms) "
            "or --window-minutes",
        )
    if args.window_start_ms < 0 or args.window_end_ms < 0:
        raise ProbeFailure("config", "window bounds must be non-negative epoch milliseconds")
    if args.window_end_ms <= args.window_start_ms:
        raise ProbeFailure("config", "window-end-ms must be greater than window-start-ms")
    return args.window_start_ms, args.window_end_ms


def parse_args(argv=None):
    # allow_abbrev=False：防止 --token 前缀匹配到 --token-file，从后门恢复命令行令牌
    parser = SafeArgumentParser(description="QCE read-only bounded probe (R2-1/R2-2)",
                                allow_abbrev=False)
    parser.add_argument("--base", default=os.environ.get("QCE_BASE", "http://127.0.0.1:40653"))
    parser.add_argument("--token-file", default="", help="file holding the QCE token (env QCE_TOKEN also works)")
    parser.add_argument("--uin", default="", help="optional: verify UIN->peerUid via /api/users/lookup")
    parser.add_argument("--peer-uid", default="", help="target friend conversation peerUid (enables bounded fetch)")
    parser.add_argument("--window-start-ms", type=int, default=0)
    parser.add_argument("--window-end-ms", type=int, default=0)
    parser.add_argument("--window-minutes", type=float, default=None,
                        help="fixed window ending at run start; mutually exclusive with ms window")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"1..{MAX_BATCH_SIZE}; explicit batchSize avoids the default pull")
    parser.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE, help="page size (limit)")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-messages", type=int, default=2000)
    parser.add_argument("--max-fetch-requests", type=int, default=16,
                        help="sub-budget for POST /api/messages/fetch only")
    parser.add_argument("--max-total-requests", type=int, default=24,
                        help="hard cap over every HTTP request this process makes")
    parser.add_argument("--max-seconds", type=float, default=60.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    parser.add_argument("--skip-get-checks", action="store_true")
    args = parser.parse_args(argv)
    problems = []
    for name in ("limit", "max_pages", "max_messages", "max_fetch_requests",
                 "max_total_requests", "max_response_bytes"):
        try:
            positive_int(name, getattr(args, name))
        except ProbeFailure as exc:
            problems.append(f"--{name.replace('_', '-')}: {exc.detail}")
    try:
        positive_int("batch-size", args.batch_size, maximum=MAX_BATCH_SIZE)
    except ProbeFailure as exc:
        problems.append(f"--batch-size: {exc.detail}")
    for name in ("max_seconds", "request_timeout"):
        try:
            positive_finite(name, getattr(args, name))
        except ProbeFailure as exc:
            problems.append(f"--{name.replace('_', '-')}: {exc.detail}")
    if args.window_minutes is not None:
        try:
            positive_finite("window-minutes", args.window_minutes)
        except ProbeFailure as exc:
            problems.append(f"--window-minutes: {exc.detail}")
    if args.max_fetch_requests > args.max_total_requests:
        problems.append("--max-fetch-requests cannot exceed --max-total-requests")
    if problems:
        # 走 SafeArgumentParser.error：只报字段名与规则，不回显被拒的值
        parser.error("; ".join(problems))
    return args


def run_probe(args) -> tuple[dict, int]:
    """返回 (summary, exit_code)。任何失败都汇总进 summary，不打印"通过"。"""
    summary: dict = {
        "mode": "bounded-probe",
        "probe": "qce_probe.py R2-1/R2-2",
        "base": None,
        "tokenSource": None,
        "checks": [],
        "checksTruncatedBy": None,
        "fetch": None,
        "status": "ok",
        "failureStage": None,
        "failureDetail": None,
    }
    budget = None
    code = 0
    try:
        budget = Budget(
            max_total_requests=positive_int(
                "max-total-requests", getattr(args, "max_total_requests", None)),
            max_fetch_requests=positive_int(
                "max-fetch-requests", getattr(args, "max_fetch_requests", None)),
            max_pages=positive_int("max-pages", getattr(args, "max_pages", None)),
            max_messages=positive_int("max-messages", getattr(args, "max_messages", None)),
            max_seconds=positive_finite("max-seconds", getattr(args, "max_seconds", None)),
            request_timeout=positive_finite("request-timeout", getattr(args, "request_timeout", None)),
        )
        positive_int("batch-size", args.batch_size, maximum=MAX_BATCH_SIZE)
        positive_int("max-response-bytes", args.max_response_bytes)
        if budget.max_fetch_requests > budget.max_total_requests:
            raise ProbeFailure("config", "max-fetch-requests exceeds max-total-requests")

        base = validate_base(args.base)
        summary["base"] = base
        token, token_source = load_token(args.token_file)
        summary["tokenSource"] = token_source
        client = Client(base, token, budget, max_response_bytes=args.max_response_bytes)
        budget.start()

        if not args.skip_get_checks:
            summary["checks"], summary["checksTruncatedBy"] = run_get_checks(client, uin=args.uin)

        if args.peer_uid:
            window = resolve_window(args)
            summary["fetch"] = bounded_fetch(client, args, window)
    except BudgetExhausted as exc:
        # 预算耗尽不是失败：兜住 fetch 之外的出口（GET 阶段后时间已到等）
        summary["checksTruncatedBy"] = summary["checksTruncatedBy"] or exc.reason
        if summary["fetch"] is None:
            summary["fetchNote"] = f"skipped: {exc.reason}"
    except ProbeFailure as exc:
        summary["status"] = "error"
        summary["failureStage"] = exc.stage
        summary["failureDetail"] = exc.detail
        code = 2
    summary["budget"] = budget.snapshot() if budget else None
    return summary, code


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        summary, code = run_probe(args)
    except Exception as exc:  # 兜底：未预期异常不得把命令行值带进 traceback
        print(f"UNEXPECTED_FAILURE: {type(exc).__name__}", file=sys.stderr)
        summary = {"status": "error", "failureStage": "unexpected",
                   "failureDetail": type(exc).__name__, "fetch": None}
        code = 2
    fetch = summary.get("fetch")
    if summary["status"] == "ok":
        if fetch and fetch["status"] == "partial":
            print(f"FETCH PARTIAL: {fetch['partialReason']}"
                  "（预算或分页异常所致，非失败；详见 SUMMARY_JSON）")
        elif fetch:
            print(f"FETCH {fetch['status']}: unique={fetch['uniqueMessageIds']}")
    else:
        print(f"PROBE FAILED at stage={summary['failureStage']}: {summary['failureDetail']}",
              file=sys.stderr)
    print("SUMMARY_JSON=" + json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())

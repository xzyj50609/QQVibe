"""R2-1/R2-2 探针行为回归（08 复核 §6 要求的"反例转成正确行为"）。

全部离线、全部合成数据，无真实 QQ/网络访问、无凭证。fake server 从同一份合成数据集
按实际参数计算过滤/分页/缓存（见 fake_qce_server.py），因此计数是可核对的算术结果。

覆盖：
  RS01 缓存增长惰性扩展 / 缓存还有页但 hasNext=false / 重复页无进展 / 空页却声称更多
  RS02 混合坏 ID 被计数 / 元数据矛盾按 protocol 失败 / 缺元数据 partial / 合法空
  RS03 条数预算收紧请求 limit / 总请求预算覆盖 GET 检查 / fetch 子预算分列 /
       deadline 收紧单请求超时 / 非法预算参数拒绝
  RS04 argparse 错误、base query、异常栈、业务 message、token 文件五处脱敏；
       --compare-default 已从 CLI 移除

运行：python probes/test_qce_probe.py（已挂入 run-python-tests.py 的 probes 组）
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from fake_qce_server import BASE_EPOCH_S, DEFAULT_TOKEN, RunningFakeQCE

PROBE = Path(__file__).resolve().parent / "qce_probe.py"
TOKEN = DEFAULT_TOKEN
WINDOW_START_MS = BASE_EPOCH_S * 1000


def window_args(matched_messages: int, *, step_seconds: int = 60) -> list[str]:
    """固定窗口，正好覆盖数据集里前 matched_messages 条（同秒样本另计）。"""
    return ["--window-start-ms", str(WINDOW_START_MS),
            "--window-end-ms", str(WINDOW_START_MS + matched_messages * step_seconds * 1000)]


def fetch_args(matched: int = 12) -> list[str]:
    return ["--peer-uid", "u_synthetic_peer", *window_args(matched)]


CLEAN = {"datasetSize": 240, "stepSeconds": 60, "sameSecondPairs": 0}


def probe_once(server: RunningFakeQCE, extra_args: list[str],
               *, env_token: str | None = TOKEN) -> tuple:
    env = dict(os.environ)
    env.pop("QCE_TOKEN", None)
    env.pop("QCE_BASE", None)
    if env_token is not None:
        env["QCE_TOKEN"] = env_token
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(PROBE), "--base", server.base, *extra_args],
        capture_output=True, text=True, env=env, timeout=120,
    )
    summary = None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("SUMMARY_JSON="):
            summary = json.loads(line[len("SUMMARY_JSON="):])
            break
    return proc.returncode, proc.stdout, proc.stderr, summary, server.requests, \
        time.monotonic() - started


def run_probe(extra_args: list[str], *, scenario: dict, env_token: str | None = TOKEN) -> tuple:
    with RunningFakeQCE(scenario) as server:
        return probe_once(server, extra_args, env_token=env_token)


def fetch_calls(requests: list) -> list:
    return [r for r in requests if r["path"] == "/api/messages/fetch"]


class PaginationTests(unittest.TestCase):
    """RS01：缓存末页不等于读完上游。"""

    def test_lazy_cache_growth_keeps_reading_past_first_page(self):
        """batchSize=3、页大小=3、窗口内 12 条：缓存必须逐次扩展并读完，而不是第 1 页就 complete。"""
        scenario = {**CLEAN, "fetch": "dataset"}
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--batch-size", "3", "--limit", "3", "--max-pages", "6",
                              "--max-fetch-requests", "8"],
            scenario=scenario)
        self.assertEqual(code, 0, err)
        fetch = summary["fetch"]
        self.assertEqual(fetch["status"], "complete", fetch["partialReason"])
        self.assertEqual(fetch["uniqueMessageIds"], 12)
        # 旧行为：第 1 页 totalPages=1 即 complete，只发 1 次请求、只读到 3 条
        self.assertEqual(len(fetch_calls(requests)), 4)
        self.assertEqual([p["totalPages"] for p in fetch["pages"]], [1, 2, 3, 4])
        self.assertEqual([p["hasNext"] for p in fetch["pages"]], [True, True, True, False])

    def test_cached_pages_are_still_read_when_hasNext_false(self):
        """缓存一次载入全部（hasNext=false）但分页未读完：继续读完缓存页才 complete。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--batch-size", "50", "--limit", "3", "--max-pages", "6",
                              "--max-fetch-requests", "8"],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        fetch = summary["fetch"]
        self.assertEqual(fetch["status"], "complete", fetch["partialReason"])
        self.assertEqual(fetch["uniqueMessageIds"], 12)
        self.assertEqual(len(fetch_calls(requests)), 4)
        self.assertTrue(all(p["hasNext"] is False for p in fetch["pages"]))
        self.assertEqual([p["totalPages"] for p in fetch["pages"]], [4, 4, 4, 4])

    def test_growing_cache_stops_as_partial_when_budget_hits(self):
        """缓存可扩展但页数预算到顶：partial 而非 complete，且明确少读了多少。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--batch-size", "3", "--limit", "3", "--max-pages", "2",
                              "--max-fetch-requests", "8"],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        fetch = summary["fetch"]
        self.assertEqual(fetch["status"], "partial")
        self.assertIn("max-pages", fetch["partialReason"])
        self.assertEqual(fetch["uniqueMessageIds"], 6)
        self.assertEqual(len(fetch_calls(requests)), 2)

    def test_duplicate_page_with_progress_claim_is_partial(self):
        """页码在前进但 ID 集合不增长：判 partial，不再谎报 complete。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--max-pages", "6", "--max-fetch-requests", "8"],
            scenario={**CLEAN, "fetch": "repeat-same"})
        self.assertEqual(code, 0, err)
        fetch = summary["fetch"]
        self.assertEqual(fetch["status"], "partial")
        self.assertIn("duplicate", fetch["partialReason"])
        self.assertEqual(fetch["uniqueMessageIds"], 3)
        self.assertEqual(len(fetch_calls(requests)), 2)

    def test_empty_page_claiming_more_is_partial(self):
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12), scenario={**CLEAN, "fetch": "empty-with-more"})
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["fetch"]["status"], "partial")
        self.assertIn("empty page while hasNext=true", summary["fetch"]["partialReason"])
        self.assertEqual(len(fetch_calls(requests)), 1)

    def test_legal_empty_window_is_complete_empty(self):
        code, out, err, summary, requests, _ = run_probe(
            ["--peer-uid", "u_synthetic_peer",
             "--window-start-ms", str((BASE_EPOCH_S + 100000) * 1000),
             "--window-end-ms", str((BASE_EPOCH_S + 100100) * 1000)],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["fetch"]["status"], "complete-empty")
        self.assertEqual(summary["fetch"]["uniqueMessageIds"], 0)

    def test_missing_pagination_metadata_is_partial(self):
        code, out, err, summary, _requests, _ = run_probe(fetch_args(12),
                                                          scenario={"fetch": "missing-metadata"})
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["fetch"]["status"], "partial")
        self.assertIn("missing pagination metadata", summary["fetch"]["partialReason"])


class StructureValidationTests(unittest.TestCase):
    """RS02：坏项计数、结构矛盾分诊。"""

    def test_mixed_invalid_ids_are_counted_not_swallowed(self):
        """合法字符串 ID 混着整数/缺失/空白 ID：必须 partial 并报出坏项数与种类。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--limit", "20"], scenario={**CLEAN, "fetch": "malformed-items"})
        self.assertEqual(code, 0, err)
        fetch = summary["fetch"]
        self.assertEqual(fetch["status"], "partial")
        self.assertNotEqual(fetch["status"], "complete")
        self.assertEqual(fetch["invalidMessageItems"], 5)
        self.assertIn("malformed message item", fetch["partialReason"])
        kinds = fetch["pages"][0]["invalidItems"]["kinds"]
        self.assertIn("msgId-type:int", kinds)
        self.assertIn("msgId-absent", kinds)
        self.assertIn("msgId-empty", kinds)
        self.assertTrue(any(k.startswith("not-object:") for k in kinds))

    def test_page_number_mismatch_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(fetch_args(12),
                                                   scenario={**CLEAN, "fetch": "page-mismatch"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("currentPage", summary["failureDetail"])

    def test_returned_count_exceeding_total_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(fetch_args(12),
                                                   scenario={**CLEAN, "fetch": "count-contradiction"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("returnedCount", summary["failureDetail"])

    def test_non_boolean_hasnext_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(fetch_args(12),
                                                   scenario={**CLEAN, "fetch": "hasnext-non-bool"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("hasNext must be a boolean", summary["failureDetail"])

    def test_string_totalpages_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(fetch_args(12),
                                                   scenario={**CLEAN, "fetch": "string-totalpages"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("non-boolean integer", summary["failureDetail"])

    def test_messages_not_array_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(fetch_args(12),
                                                   scenario={**CLEAN, "fetch": "messages-not-array"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("messages must be an array", summary["failureDetail"])

    def test_same_second_two_ids_both_kept(self):
        """X1 形态：同一秒两条不同原生 ID 都计入唯一集，不被合并。"""
        scenario = {**CLEAN, "sameSecondPairs": 3, "fetch": "dataset"}
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--batch-size", "50", "--limit", "3", "--max-pages", "8",
                              "--max-fetch-requests", "10"],
            scenario=scenario)
        self.assertEqual(code, 0, err)
        # 窗口 12×60s 内含锚点 j=0(t+0s)、j=1(t+420s) 的同秒副本 → 14 条
        self.assertEqual(summary["fetch"]["status"], "complete", summary["fetch"]["partialReason"])
        self.assertEqual(summary["fetch"]["uniqueMessageIds"], 14)


class BudgetTests(unittest.TestCase):
    """RS03：预算是实际的执行上限。"""

    def test_message_budget_tightens_request_limit_before_sending(self):
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--max-messages", "1", "--limit", "50", "--batch-size", "50"],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        calls = fetch_calls(requests)
        self.assertEqual(calls[0]["body"]["limit"], 1)      # 请求前收紧，不是回多了再默默接受
        self.assertEqual(calls[0]["body"]["batchSize"], 1)
        fetch = summary["fetch"]
        self.assertLessEqual(fetch["uniqueMessageIds"], 1)
        self.assertEqual(fetch["status"], "partial")
        self.assertIn("max-messages", fetch["partialReason"])

    def test_total_request_budget_covers_get_checks(self):
        """GET 检查同样过账：max-total-requests=2 时整个进程只发 2 次请求。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--uin", "10001", "--max-total-requests", "2",
                              "--max-fetch-requests", "2"],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(requests), 2)
        self.assertEqual(summary["budget"]["usedTotalRequests"], 2)
        self.assertTrue(summary["checksTruncatedBy"] or summary["fetch"],
                        "预算应在 GET 或 fetch 处显式留下痕迹")
        if summary["fetch"]:
            self.assertEqual(summary["fetch"]["status"], "partial")
            self.assertIn("max-total-requests", summary["fetch"]["partialReason"])

    def test_fetch_sub_budget_is_reported_separately(self):
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--batch-size", "3", "--limit", "3", "--max-pages", "6",
                              "--max-fetch-requests", "1", "--max-total-requests", "20"],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fetch_calls(requests)), 1)
        self.assertEqual(summary["fetch"]["requestsUsed"], 1)
        self.assertEqual(summary["budget"]["usedFetchRequests"], 1)
        self.assertEqual(summary["budget"]["usedTotalRequests"], 5)  # 4 次 GET 检查 + 1 次 fetch
        self.assertIn("max-fetch-requests", summary["fetch"]["partialReason"])

    def test_deadline_clamps_per_request_timeout(self):
        """max-seconds 小于服务端延迟：必须很快返回 partial，绝不 30 秒超时后 complete。"""
        code, out, err, summary, requests, elapsed = run_probe(
            fetch_args(12) + ["--max-seconds", "0.05", "--request-timeout", "30"],
            scenario={**CLEAN, "fetch": "dataset", "delayMs": 400})
        self.assertLess(elapsed, 10, f"deadline 未收紧单请求超时：{elapsed}")
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["fetch"]["status"], "partial")
        self.assertIn("max-seconds", summary["fetch"]["partialReason"])

    def test_window_filter_actually_limits_rows(self):
        """fake 按请求窗口过滤：窗口只含 3 条时探针不得读到 240 条。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(3) + ["--batch-size", "500", "--limit", "50", "--max-pages", "5",
                             "--max-fetch-requests", "8"],
            scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        self.assertEqual(summary["fetch"]["uniqueMessageIds"], 3)
        self.assertEqual(summary["fetch"]["serverReportedTotalFirstPage"], 3)

    def test_invalid_budget_arguments_rejected_before_any_request(self):
        bad = [
            ["--max-seconds", "0"], ["--max-seconds", "nan"], ["--max-seconds", "inf"],
            ["--max-pages", "-1"], ["--limit", "0"], ["--batch-size", "5001"],
            ["--max-fetch-requests", "9", "--max-total-requests", "2"],
            ["--request-timeout", "abc"], ["--max-response-bytes", "0"],
            ["--window-minutes", "0"],
        ]
        for extra in bad:
            with self.subTest(args=extra):
                code, out, err, summary, _req, _ = run_probe(
                    fetch_args(12) + extra, scenario={**CLEAN, "fetch": "dataset"})
                self.assertEqual(code, 2, f"{extra}: {out}{err}")
                self.assertEqual(summary, None, "参数错误应在发请求前退出，不产生 SUMMARY_JSON")
                self.assertEqual(err.count("Traceback"), 0)


class CredentialSafetyTests(unittest.TestCase):
    """RS04：stdout / stderr / SUMMARY_JSON / 异常栈 四处出口统一脱敏。"""

    def test_command_line_token_is_rejected_and_never_echoed(self):
        with RunningFakeQCE({**CLEAN, "fetch": "dataset"}) as server:
            env = dict(os.environ)
            env.pop("QCE_TOKEN", None)
            proc = subprocess.run(
                [sys.executable, str(PROBE), "--base", server.base, *fetch_args(12),
                 "--token", TOKEN],
                capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(TOKEN, proc.stdout)
        self.assertNotIn(TOKEN, proc.stderr)          # 旧版只查 stdout，stderr 里原文回显
        self.assertNotIn(TOKEN, proc.stderr.lower())
        self.assertIn("command line could not be parsed", proc.stderr)

    def test_base_with_query_userinfo_or_path_is_rejected_without_echo(self):
        for bad in (f"http://127.0.0.1:9/?token={TOKEN}",
                    f"http://evil:9/{TOKEN}",
                    f"http://{TOKEN}@127.0.0.1:9",
                    f"http://127.0.0.1:9#{TOKEN}",
                    "http://10.0.0.1:40653", "http://0.0.0.0:40653", "ftp://127.0.0.1:21",
                    "http://127.0.0.1:99999999"):
            with self.subTest(base=bad):
                code, out, err, summary, requests, _ = run_probe(
                    ["--base", bad, *fetch_args(12)], scenario={**CLEAN, "fetch": "dataset"})
                self.assertEqual(code, 2)
                self.assertEqual(requests, [])
                self.assertNotIn(TOKEN, out)
                self.assertNotIn(TOKEN, err)
                if summary:
                    self.assertNotIn(TOKEN, json.dumps(summary))
                    self.assertEqual(summary["failureStage"], "config")

    def test_summary_never_carries_the_token_on_success(self):
        code, out, err, summary, _req, _ = run_probe(fetch_args(12),
                                                     scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, err)
        self.assertNotIn(TOKEN, json.dumps(summary))
        self.assertEqual(summary["tokenSource"], "env")

    def test_nested_error_envelope_code_extracted_without_message(self):
        """QCE 正式错误 envelope 是嵌套 error 对象；服务端 message 里的诱饵不得被复制。"""
        code, out, err, summary, _req, _ = run_probe(
            fetch_args(12), scenario={**CLEAN, "fetch": "business-false-nested"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "business")
        self.assertIn("UPSTREAM_TIMEOUT", summary["failureDetail"])
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, err)
        self.assertNotIn(TOKEN, json.dumps(summary))

    def test_flat_error_envelope_message_is_not_copied(self):
        code, out, err, summary, _req, _ = run_probe(
            fetch_args(12), scenario={**CLEAN, "fetch": "business-leaky-message"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "business")
        self.assertIn("AUTH_HEADER_REJECTED", summary["failureDetail"])
        self.assertNotIn(TOKEN, json.dumps(summary))
        self.assertNotIn("from uin", summary["failureDetail"])

    def test_unclassified_when_error_code_is_not_a_bounded_token(self):
        code, out, err, summary, _req, _ = run_probe(fetch_args(12),
                                                    scenario={"fetch": "business-false-flat"})
        self.assertEqual(code, 2)
        self.assertIn("CACHE_MISS_UPSTREAM", summary["failureDetail"])

    def test_token_file_read_failure_is_sanitized(self):
        missing = str(Path(__file__).resolve().parent / "definitely-missing-token-file.txt")
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--token-file", missing],
            scenario={**CLEAN, "fetch": "dataset"}, env_token=None)
        self.assertEqual(code, 2)
        self.assertEqual(requests, [])
        self.assertEqual(summary["failureStage"], "config")
        self.assertIn("token file could not be read", summary["failureDetail"])
        self.assertNotIn("definitely-missing-token-file", json.dumps(summary))
        self.assertNotIn(missing, err)

    def test_no_token_is_config_failure(self):
        code, out, err, summary, requests, _ = run_probe(fetch_args(12),
                                                         scenario={}, env_token=None)
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "config")
        self.assertEqual(requests, [])

    def test_unexpected_exception_stack_does_not_leak_values(self):
        """异常栈出口：未预期异常的 message 可能带凭证，只允许输出异常类型名。"""
        sys.path.insert(0, str(PROBE.parent))
        import qce_probe
        buffer_out, buffer_err = io.StringIO(), io.StringIO()
        with mock.patch.object(qce_probe, "run_probe",
                               side_effect=RuntimeError(f"boom {TOKEN}")):
            with contextlib.redirect_stdout(buffer_out), contextlib.redirect_stderr(buffer_err):
                code = qce_probe.main(["--base", "http://127.0.0.1:9", "--skip-get-checks"])
        self.assertEqual(code, 2)
        self.assertNotIn(TOKEN, buffer_out.getvalue())
        self.assertNotIn(TOKEN, buffer_err.getvalue())
        self.assertIn("UNEXPECTED_FAILURE: RuntimeError", buffer_err.getvalue())

    def test_compare_default_option_is_gone_from_the_cli(self):
        """RS03：真实可访问 QCE 的探针不再提供无界的默认对照入口。"""
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--compare-default"], scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 2)
        self.assertEqual(summary, None)
        self.assertEqual(requests, [])
        self.assertNotIn(TOKEN, err)
        source = PROBE.read_text(encoding="utf-8")
        self.assertNotIn('add_argument("--compare-default"', source)
        self.assertNotIn("default_comparison", source)


class TransportSafetyTests(unittest.TestCase):
    def test_401_is_hard_auth_failure_without_retry(self):
        code, out, err, summary, requests, _ = run_probe(fetch_args(12),
                                                         scenario={"fetch": "auth-401"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "auth")
        self.assertNotIn("通过", out)
        self.assertEqual(len(fetch_calls(requests)), 1)
        self.assertTrue(all("token=" not in r["query"] for r in requests))

    def test_bad_json_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(fetch_args(12), scenario={"fetch": "bad-json"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")

    def test_oversized_response_is_protocol_error(self):
        code, out, err, summary, _r, _ = run_probe(
            fetch_args(12) + ["--max-response-bytes", "10000"], scenario={"fetch": "huge"})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("max-response-bytes", summary["failureDetail"])

    def test_redirect_is_refused(self):
        code, out, err, summary, _r, _ = run_probe([], scenario={"redirect": True})
        self.assertEqual(code, 2)
        self.assertEqual(summary["failureStage"], "protocol")
        self.assertIn("redirect refused", summary["failureDetail"])

    def test_string_chattype_is_rejected_by_qce_semantics(self):
        """RV01 锚点：探针只发数字 chatType=1，fake 与实装一致地拒绝字符串。"""
        code, out, err, summary, requests, _ = run_probe(fetch_args(12),
                                                         scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        for call in fetch_calls(requests):
            self.assertIsInstance(call["body"]["peer"]["chatType"], int)
            self.assertEqual(call["body"]["peer"]["chatType"], 1)

    def test_fixed_window_cache_hit_on_identical_bounded_request(self):
        scenario = {**CLEAN, "fetch": "dataset"}
        with RunningFakeQCE(scenario) as server:
            args = fetch_args(12) + ["--batch-size", "50", "--limit", "3", "--max-pages", "8",
                                     "--max-fetch-requests", "10"]
            code1, _, err1, summary1, _r1, _t = probe_once(server, args)
            self.assertEqual(code1, 0, err1)
            self.assertFalse(summary1["fetch"]["pages"][0]["cacheHit"])
            server.requests.clear()
            code2, _, err2, summary2, _r2, _t = probe_once(server, args)
            self.assertEqual(code2, 0, err2)
            self.assertTrue(summary2["fetch"]["pages"][0]["cacheHit"])
            self.assertEqual(summary2["fetch"]["uniqueMessageIds"],
                             summary1["fetch"]["uniqueMessageIds"])

    def test_contract_fields_on_a_normal_page(self):
        code, out, err, summary, requests, _ = run_probe(
            fetch_args(12) + ["--uin", "10001"], scenario={**CLEAN, "fetch": "dataset"})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(summary["checks"]), 5)
        calls = fetch_calls(requests)
        body = calls[0]["body"]
        self.assertEqual(body["batchSize"], 200)
        self.assertEqual(body["filter"]["startTime"], WINDOW_START_MS)
        self.assertEqual(body["filter"]["endTime"], WINDOW_START_MS + 12 * 60 * 1000)
        page = summary["fetch"]["pages"][0]
        self.assertEqual(page["messagesSummary"]["idShapes"]["msgId"], {"type": "str", "length": 19})
        self.assertEqual(page["messagesSummary"]["timeShapes"]["msgTime"],
                         {"type": "str", "length": 10})
        self.assertNotIn("text", json.dumps(summary))  # 探针不输出正文


if __name__ == "__main__":
    unittest.main()

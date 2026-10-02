"""T07 local-history behavior using temporary QQ and result databases."""
from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

import qq_history_browser as history
from backend_contracts import AccountChangedError
from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from model_source import ModelSourceStore
from qq_source import CURSOR_VERSION
from result_store import ResultStore
from test_api_insights import Analyzer as ApiAnalyzer
from test_qq_local_integrity import Library
from test_qq_message_store import CONV, record


class LocalHistoryTests(Library):
    def seed(self, count=9):
        self.ingest(*(record(str(i), time_ms=1000 + (i // 2) * 1000, text=f"text {i}")
                      for i in range(count)))
        self.ingest(record("system", time_ms=1, direction="system"))

    def test_paging_reassembles_all_visible_ids_without_losing_same_second_rows(self):
        self.seed()
        before, pages = None, []
        for _ in range(8):
            page = history.browse(self.source, self.account, CONV, before=before, limit=3)
            pages.insert(0, [item["id"] for item in page["messages"]])
            if not page["hasMoreBefore"]:
                break
            self.assertIsNotNone(page["nextCursor"])
            self.assertNotEqual(before, page["nextCursor"])
            before = page["nextCursor"]
        else:
            self.fail("history did not exhaust")
        self.assertEqual([identifier for page in pages for identifier in page], [str(i) for i in range(9)])

    def test_around_a_search_hit_returns_neighbors_and_a_focus_id(self):
        self.seed()
        found = history.search(self.source, self.account, CONV, query="text 4")
        anchor = found["messages"][0]["historyCursor"]
        page = history.browse(self.source, self.account, CONV, around=anchor, limit=5)
        self.assertEqual(page["focusId"], "4")
        self.assertEqual([row["id"] for row in page["messages"]], ["2", "3", "4", "5", "6"])
        self.assertTrue(page["hasMoreBefore"])
        self.assertTrue(page["hasMoreAfter"])

    def test_empty_conversation_and_page_before_the_first_message(self):
        self.assertEqual(history.browse(self.source, self.account, CONV)["messages"], [])
        self.seed(1)
        anchor = self.source.messages(CONV, 10)[0]["historyCursor"]
        page = history.browse(self.source, self.account, CONV, before=anchor)
        self.assertEqual(page["messages"], [])
        self.assertFalse(page["hasMoreBefore"])

    def test_search_casefolds_unicode_and_treats_sql_patterns_as_literal_text(self):
        self.ingest(record("1", text="Straße 明天"), record("2", text="100%"))
        self.assertEqual(history.search(self.source, self.account, CONV, query="STRASSE")["messages"][0]["id"], "1")
        self.assertEqual(history.search(self.source, self.account, CONV, query="%")["messages"][0]["id"], "2")
        self.assertEqual(history.search(self.source, self.account, CONV, query="' OR 1=1 --")["messages"], [])

    def test_search_never_returns_withheld_body_or_quoted_text_as_the_senders_words(self):
        self.ingest(record("1", text="private recalled body", status="recalled", recall_time="5"),
                    record("2", text="private disputed body", status="conflict"),
                    record("3", text="自己的话", quote="private quote"))
        self.assertEqual(history.search(self.source, self.account, CONV, query="private")["messages"], [])
        recalled = history.search(self.source, self.account, CONV, query="撤回")["messages"]
        self.assertEqual((recalled[0]["id"], recalled[0]["kind"]), ("1", "other"))

    def test_search_scan_budget_advances_even_when_a_page_has_no_match(self):
        self.ingest(*(record(str(i), time_ms=i + 1, text="needle" if i == 0 else "haystack") for i in range(5)))
        cursor, found = None, []
        with patch.object(history, "SEARCH_SCAN_LIMIT", 2):
            for _ in range(4):
                page = history.search(self.source, self.account, CONV, query="needle", before=cursor)
                found += [row["id"] for row in page["messages"]]
                if not page["hasMore"]:
                    break
                self.assertIsNotNone(page["nextCursor"])
                self.assertNotEqual(cursor, page["nextCursor"])
                cursor = page["nextCursor"]
            else:
                self.fail("search cursor did not exhaust")
        self.assertEqual(found, ["0"])

    def test_date_search_uses_millisecond_half_open_boundaries(self):
        bounds = history.date_bounds("2026-09-29")
        start, end = bounds[2:]
        self.ingest(record("before", time_ms=start - 1), record("start", time_ms=start),
                    record("last", time_ms=end - 1), record("after", time_ms=end))
        result = history.search(self.source, self.account, CONV, day="2026-09-29")
        self.assertEqual([row["id"] for row in result["messages"]], ["last", "start"])

    def test_scope_and_numeric_cursor_validation_precede_sql(self):
        self.seed(1)
        anchor = self.source.messages(CONV, 10)[0]["historyCursor"]
        with self.assertRaises(ValueError):
            history.browse(self.source, self.account, "u:foreign", before=anchor)
        with self.assertRaises(AccountChangedError):
            history.browse(self.source, "a:" + "f" * 32, CONV)
        for timestamp, version in ((2**80, CURSOR_VERSION), (1000, True)):
            raw = json.dumps([version, "qq", self.account, CONV, timestamp, 1,
                              self.store.revision(self.account, CONV)[0]]).encode()
            bad = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
            with self.assertRaises(ValueError):
                history.browse(self.source, self.account, CONV, before=bad)
        with self.assertRaises(ValueError):
            history.browse(self.source, self.account, CONV, limit=0)
        with self.assertRaises(ValueError):
            history.search(self.source, self.account, CONV)

    def test_backfill_and_recall_invalidate_external_history_cursors_but_append_does_not(self):
        self.ingest(record("1", time_ms=1000), record("2", time_ms=2000))
        cursor = history.browse(self.source, self.account, CONV, limit=1)["nextCursor"]
        self.ingest(record("3", time_ms=3000))
        self.assertEqual(history.browse(self.source, self.account, CONV, before=cursor)["messages"][0]["id"], "1")
        self.ingest(record("0", time_ms=500))
        for operation in (lambda: history.browse(self.source, self.account, CONV, before=cursor),
                          lambda: history.browse(self.source, self.account, CONV, around=cursor),
                          lambda: history.search(self.source, self.account, CONV, query="text", before=cursor)):
            with self.assertRaisesRegex(ValueError, "stale-history-cursor"):
                operation()
        cursor = history.browse(self.source, self.account, CONV)["newestCursor"]
        self.ingest(record("2", time_ms=2000, status="recalled", recall_time="5"))
        with self.assertRaisesRegex(ValueError, "stale-history-cursor"):
            self.source.preceding_text_context(CONV, cursor)


class BackendHistoryTests(Library):
    def setUp(self):
        super().setUp()
        self.ingest(*(record(str(i), time_ms=1000 + i, text=f"旧消息 {i}") for i in range(5)))
        self.analyzer = ApiAnalyzer()
        self.analyzer.model = {"state": "ready"}
        self.analyzer.analysis_version = lambda: "synthetic-v1"
        self.models = ModelSourceStore(self.root / "model-source.json", root=self.root,
                                       protect=lambda key: b"synthetic:" + key.encode(),
                                       unprotect=lambda value: value.removeprefix(b"synthetic:").decode())
        self.results = ResultStore(self.root / "results.sqlite")
        self.backend = Backend(self.source, analyzer=self.analyzer, model_source_store=self.models,
                               store_factory=lambda *_: self.results,
                               selection_store=ConversationSelectionStore(self.root / "selection"))
        self.addCleanup(self.backend.shutdown)

    def test_ordinary_history_and_search_use_qq_without_wechat_private_members(self):
        with patch("backend_service.browse_history", side_effect=AssertionError("WeChat route")), \
                patch("backend_service.search_history", side_effect=AssertionError("WeChat route")):
            page = self.backend.history(self.account, CONV, limit=2)
            self.assertEqual([row["id"] for row in page["messages"]], ["3", "4"])
            result = self.backend.history_search(self.account, CONV, query="旧消息 1")
            self.assertEqual(result["messages"][0]["id"], "1")
        self.assertFalse(any(key.startswith("_") for row in page["messages"] for key in row))

    def test_api_insight_around_resolves_real_qq_history_then_calls_only_fake_analyzer(self):
        self.backend.model_source_activate({"mode": "api", "protocol": "responses",
                                            "baseUrl": "https://example.test/v1", "model": "synthetic",
                                            "apiKey": "synthetic-key", "contextTokens": 8192})
        anchor = self.source.messages(CONV, 10)[0]["historyCursor"]
        with patch("backend_service.browse_history", side_effect=AssertionError("WeChat route")):
            self.backend.start_model_insights(self.account, CONV, 1, ["0"], anchor)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.backend.model_insights(CONV, ids=["0"])
            if result["job"]["status"] in ("done", "error"):
                break
            time.sleep(.01)
        self.assertEqual(result["job"]["status"], "done", result)
        self.assertEqual(self.analyzer.calls[-1][3], ("0",))


if __name__ == "__main__":
    unittest.main(verbosity=2)

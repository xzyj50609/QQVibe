"""QQ API revisions with real workers/repositories and deterministic fake providers."""
import copy
import threading
import unittest
from unittest.mock import patch

from backend_contracts import api_insight_scope, api_portrait_scope
from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from model_source import ModelSourceStore
from result_store import ResultStore
from test_api_insights import Analyzer as BaseAnalyzer
from test_qq_local_integrity import Library
from test_qq_message_store import CONV, record


class Analyzer(BaseAnalyzer):
    model = {"state": "ready"}
    def __init__(self):
        super().__init__()
        self.block_next = None
        self.gate_entered, self.gate_release = threading.Event(), threading.Event()

    def analysis_version(self):
        return "synthetic-local-v1"

    def gate(self, kind):
        if self.block_next == kind:
            self.block_next = None
            self.gate_entered.set()
            if not self.gate_release.wait(5):
                raise RuntimeError("synthetic provider timeout")

    def model_insights(self, *args, **kwargs):
        self.gate("insights")
        return super().model_insights(*args, **kwargs)

    def model_portrait(self, *args):
        self.gate("portrait")
        result = super().model_portrait(*args)
        previous, messages = args[-2:]
        result["summary"] = "|".join([*([previous["summary"]] if previous["summary"] else []),
                                      *(row["id"] for row in messages)])
        return result

    def refresh_portrait_axes(self, *args):
        self.gate("axes")
        return super().refresh_portrait_axes(*args)

    def close(self):
        self.gate_release.set()


class ApiRevisionTests(Library):
    def setUp(self):
        super().setUp()
        self.ingest(record("late1", time_ms=3000, text="后来的消息"),
                    record("late2", time_ms=4000, text="再后来的消息"))
        self.results = ResultStore(self.root / "results.sqlite3")
        self.analyzer = Analyzer()
        self.models = ModelSourceStore(self.root / "models.json", root=self.root,
            protect=lambda value: value.encode(), unprotect=lambda value: value.decode())
        self.backend = Backend(self.source, analyzer=self.analyzer, model_source_store=self.models,
            store_factory=lambda *_: self.results,
            selection_store=ConversationSelectionStore(self.root / "selection"))
        self.addCleanup(self.backend.shutdown)
        self.source_id = self.activate("model-a")

    def activate(self, name):
        self.backend.model_source_activate({"mode": "api", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": name, "apiKey": "synthetic-only",
            "contextTokens": 8192})
        return self.backend.active_model_source_id

    def idle(self):
        self.assertTrue(self.backend.api_tasks.wait_for_idle(10), "API work did not drain")

    def insights(self, ids=None):
        result = self.backend.start_model_insights(self.account, CONV, 20, ids)
        self.idle()
        job = self.backend.api_jobs[(self.account, CONV, self.backend.active_model_source_id)]
        self.assertEqual(job["status"], "done", job)
        return self.backend.model_insights(CONV)

    def portrait(self, expected="done"):
        self.backend.start_model_portrait(self.account, CONV)
        self.idle()
        job = self.backend.api_portrait_jobs[(self.account, CONV, self.backend.active_model_source_id, CONV)]
        self.assertEqual(job["status"], expected, job)
        return self.results.api_portrait_get(self.account, CONV,
            api_portrait_scope(self.backend.active_model_source_id), CONV)

    def test_same_context_is_reused_but_appended_context_reanalyzes_existing_targets(self):
        self.assertEqual(set(self.insights()["results"]), {"late1", "late2"})
        calls = len(self.analyzer.calls)
        self.insights()
        self.assertEqual(len(self.analyzer.calls), calls)
        self.ingest(record("new", time_ms=5000))
        changed = self.backend.model_insights(CONV)
        self.assertTrue(changed["stale"])
        self.assertEqual(set(changed["previousResults"]), {"late1", "late2"})
        self.assertEqual(changed["results"], {})
        self.insights()
        self.assertEqual(set(self.analyzer.calls[-1][-1]), {"late1", "late2", "new"})

    def test_backfill_with_unchanged_highwater_invalidates_labels_and_old_job(self):
        self.insights()
        watermark = self.source.history_highwater(CONV)
        self.ingest(record("early", time_ms=1000))
        self.assertEqual(self.source.history_highwater(CONV), watermark)
        stale = self.backend.model_insights(CONV)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["job"]["status"], "idle")
        self.assertEqual(set(self.insights()["results"]), {"late1", "late2", "early"})

    def test_late_insight_response_from_old_revision_never_replaces_published_rows(self):
        self.insights()
        previous = self.results.api_insight_view(self.account, CONV, api_insight_scope(self.source_id))
        self.ingest(record("early", time_ms=1000))
        self.analyzer.block_next = "insights"
        self.backend.start_model_insights(self.account, CONV, 20)
        self.assertTrue(self.analyzer.gate_entered.wait(3))
        self.ingest(record("earlier", time_ms=500))
        self.analyzer.gate_release.set()
        self.idle()
        self.assertEqual(self.results.api_insight_view(self.account, CONV, api_insight_scope(self.source_id)), previous)
        self.assertEqual(self.backend.model_insights(CONV)["job"]["status"], "idle")
        self.insights()

    def test_portrait_rebuild_starts_from_empty_and_retains_old_until_complete(self):
        before = self.portrait()
        self.ingest(record("early", time_ms=1000))
        self.analyzer.block_next = "portrait"
        self.backend.start_model_portrait(self.account, CONV)
        self.assertTrue(self.analyzer.gate_entered.wait(3))
        try:
            view = self.backend.model_portrait(CONV)
            self.assertTrue(view["stale"])
            self.assertEqual(view["portrait"], before["portrait"])
            self.assertFalse(view["progress"]["complete"])
        finally:
            self.analyzer.gate_release.set()
        self.idle()
        after = self.results.api_portrait_get(self.account, CONV, api_portrait_scope(self.source_id), CONV)
        self.assertEqual(after["portrait"]["summary"], "early:0|late1:0|late2:0")
        self.assertEqual(after["processed"], 3)
        self.assertFalse(self.backend.model_portrait(CONV)["stale"])

    def test_failed_portrait_resumes_staged_batches_after_repository_restart(self):
        before = self.portrait()
        self.ingest(record("early", time_ms=1000))
        self.analyzer.fail_portrait_at = len(self.analyzer.portrait_calls) + 2
        with patch("backend_service.api_portrait_plan", side_effect=lambda pieces, _: list(range(1, len(pieces) + 1))):
            failed = self.portrait(expected="error")
            self.assertEqual(failed, before)
            self.backend.qq_api_stores.clear()
            start = len(self.analyzer.portrait_calls)
            after = self.portrait()
        self.assertEqual(self.analyzer.portrait_calls[start][-1][0]["id"], "late1:0")
        self.assertEqual(after["portrait"]["summary"], "early:0|late1:0|late2:0")

    def test_inventory_counts_refresh_when_backfill_does_not_move_highwater(self):
        self.backend.model_portrait(CONV)
        self.idle()
        before = self.backend.model_portrait(CONV)
        self.assertEqual(before["available"]["textCount"], 2)
        self.ingest(record("early", time_ms=1000))
        pending = self.backend.model_portrait(CONV)
        self.assertFalse(pending["inventoryReady"])
        self.idle()
        self.assertEqual(self.backend.model_portrait(CONV)["available"]["textCount"], 3)
        self.assertEqual(self.analyzer.portrait_calls, [])

    def test_inventory_inflight_old_revision_cannot_be_saved_or_reused_as_pieces(self):
        entered, release = threading.Event(), threading.Event()
        original = self.source.history_page
        def blocked_page(*args, **kwargs):
            page = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                release.wait(4)
            return page
        with patch.object(self.source, "history_page", side_effect=blocked_page):
            self.backend.model_portrait(CONV)
            self.assertTrue(entered.wait(3))
            self.ingest(record("early", time_ms=1000))
            release.set()
            self.idle()
        self.assertIsNone(self.backend.api_portrait_inventory_pieces)
        self.backend.model_portrait(CONV)
        self.idle()
        self.assertEqual(self.backend.model_portrait(CONV)["available"]["textCount"], 3)

    def test_late_axes_refresh_cannot_mutate_old_portrait_after_recall(self):
        before = self.portrait()
        self.analyzer.block_next = "axes"
        self.backend.start_model_portrait(self.account, CONV, refresh_axes=True)
        self.assertTrue(self.analyzer.gate_entered.wait(3))
        self.ingest(record("late1", time_ms=3000, text="后来的消息", status="recalled", recall_time="7"))
        self.analyzer.gate_release.set()
        self.idle()
        self.assertEqual(self.results.api_portrait_get(self.account, CONV, api_portrait_scope(self.source_id), CONV), before)
        self.assertEqual(self.portrait()["portrait"]["summary"], "late2:0")

    def test_insight_publication_failure_preserves_old_and_retry_does_not_call_provider_again(self):
        self.insights()
        before = self.results.api_insight_view(self.account, CONV, api_insight_scope(self.source_id))
        self.ingest(record("early", time_ms=1000))
        with self.results.connect() as conn:
            conn.execute("CREATE TRIGGER block_api_publish BEFORE INSERT ON api_insights_v1 "
                         "BEGIN SELECT RAISE(ABORT,'synthetic publish failure'); END")
        self.backend.start_model_insights(self.account, CONV, 20)
        self.idle()
        self.assertEqual(self.results.api_insight_view(self.account, CONV, api_insight_scope(self.source_id)), before)
        calls = len(self.analyzer.calls)
        with self.results.connect() as conn:
            conn.execute("DROP TRIGGER block_api_publish")
        self.insights()
        self.assertEqual(len(self.analyzer.calls), calls)

    def test_source_clear_is_scoped_and_revokes_running_work_and_stages(self):
        self.insights()
        a = self.source_id
        b = self.activate("model-b")
        self.insights()
        self.activate("model-a")
        self.ingest(record("early", time_ms=1000))
        self.analyzer.block_next = "insights"
        self.backend.start_model_insights(self.account, CONV, 20)
        self.assertTrue(self.analyzer.gate_entered.wait(3))
        self.backend.analysis_cache_clear(self.account, a)
        self.analyzer.gate_release.set()
        self.idle()
        self.assertEqual(self.results.api_insight_view(self.account, CONV, api_insight_scope(a)), {})
        self.assertTrue(self.results.api_insight_view(self.account, CONV, api_insight_scope(b)))
        self.insights()

    def test_portrait_context_is_part_of_insight_cache_basis(self):
        self.insights()
        self.portrait()
        self.assertEqual(self.backend.model_insights(CONV)["results"], {})
        self.insights()
        expected = "late1:0|late2:0"
        self.assertEqual(self.analyzer.insight_payloads[-1][0]["portraitContext"], expected)
        self.assertEqual(len(self.backend.model_insights(CONV)["results"]), 2)

    def test_history_anchor_is_used_for_read_basis_and_stale_anchor_requests_refresh(self):
        anchor = self.source.messages(CONV, 10)[0]["historyCursor"]
        self.backend.start_model_insights(self.account, CONV, 1, ["late1"], anchor)
        self.idle()
        self.assertIn("late1", self.backend.model_insights(CONV, ["late1"], around=anchor)["results"])
        self.ingest(record("early", time_ms=1000))
        stale = self.backend.model_insights(CONV, ["late1"], around=anchor)
        self.assertTrue(stale["refreshRequired"])
        self.assertEqual(stale["results"], {})

    def test_model_switch_a_b_a_rejects_the_old_a_response_even_with_the_same_data_revision(self):
        self.insights()
        self.ingest(record("new", time_ms=5000))
        self.analyzer.block_next = "insights"
        self.backend.start_model_insights(self.account, CONV, 20)
        old_job = self.backend.api_jobs[(self.account, CONV, self.source_id)]
        self.assertTrue(self.analyzer.gate_entered.wait(3))
        self.activate("model-b")
        self.ingest(record("newer", time_ms=6000))
        self.activate("model-a")
        self.backend.start_model_insights(self.account, CONV, 20)
        new_job = self.backend.api_jobs[(self.account, CONV, self.source_id)]
        self.assertIsNot(old_job, new_job)
        self.analyzer.gate_release.set()
        self.idle()
        self.assertEqual(old_job["status"], "error")
        self.assertEqual(new_job["status"], "done", new_job)
        self.assertEqual(set(self.backend.model_insights(CONV)["results"]), {"late1", "late2", "new", "newer"})

    def test_first_partial_portrait_is_visible_but_axes_refresh_waits_for_completion(self):
        self.analyzer.fail_portrait_at = 2
        with patch("backend_service.api_portrait_plan", side_effect=lambda pieces, _: list(range(1, len(pieces) + 1))):
            self.assertIsNone(self.portrait(expected="error"))
            data = self.backend.model_portrait(CONV)
            self.assertEqual(data["portrait"]["summary"], "late1:0")
            self.assertFalse(data["hasPublishedAnalysis"])
            self.assertFalse(data["progress"]["complete"])
            with self.assertRaisesRegex(ValueError, "finish-portrait"):
                self.backend.start_model_portrait(self.account, CONV, refresh_axes=True)
            self.assertEqual(self.analyzer.axes_calls, 0)

    def test_portrait_publish_failure_preserves_old_and_retries_completed_stage_without_model_calls(self):
        before = self.portrait()
        self.ingest(record("early", time_ms=1000))
        with self.results.connect() as conn:
            conn.execute("CREATE TRIGGER block_portrait_publish BEFORE INSERT ON api_portrait_v1 "
                         "BEGIN SELECT RAISE(ABORT,'synthetic publish failure'); END")
        self.assertEqual(self.portrait(expected="error"), before)
        calls = len(self.analyzer.portrait_calls)
        with self.results.connect() as conn:
            conn.execute("DROP TRIGGER block_portrait_publish")
        after = self.portrait()
        self.assertEqual(len(self.analyzer.portrait_calls), calls)
        self.assertEqual(after["portrait"]["summary"], "early:0|late1:0|late2:0")

    def test_large_history_reads_use_the_posted_context_instead_of_latest_window(self):
        self.ingest(*(record(str(i), time_ms=5000 + i, text="合成消息") for i in range(600)))
        oldest = self.backend.history(self.account, CONV, limit=200)["oldestCursor"]
        page = self.backend.history(self.account, CONV, before=oldest, limit=200)
        target = page["messages"][0]
        anchor = target["historyCursor"]
        self.backend.start_model_insights(self.account, CONV, 1, [target["id"]], anchor)
        self.idle()
        exact = self.backend.model_insights(CONV, [target["id"]], around=anchor)
        self.assertIn(target["id"], exact["results"])
        self.assertEqual(self.backend.model_insights(CONV, [target["id"]])["results"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

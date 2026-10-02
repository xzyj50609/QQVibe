"""Real Backend/BatchEngine + SQLite revision rebuilds; deterministic fake model only."""
import copy
import sqlite3
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from backend_contracts import FINE_LABEL_SCHEMA, LOCAL_SOURCE_ID
from backend_service import Backend
from batch_engine import BatchEngine
from conversation_selection import ConversationSelectionStore
from model_source import ModelSourceStore
from profile_state import empty_state
from qq_analysis_store import QQAnalysisStore, StaleAnalysisError
from result_store import ResultStore
from test_qq_local_integrity import Library
from test_qq_message_store import CONV, record

VERSION = "synthetic-revision-v1"


class Analyzer:
    model = {"state": "ready", "provider": "cpu"}
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.block_next = False
        self.entered, self.release = threading.Event(), threading.Event()

    def analysis_version(self):
        return VERSION

    def close(self):
        self.release.set()

    def analyze_batch(self, _session, payload, context):
        self.calls.append(copy.deepcopy((payload, context)))
        if self.block_next:
            self.block_next = False
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("synthetic test model timeout")
        if self.fail_at == len(self.calls):
            raise RuntimeError("synthetic inference failure")
        item = payload[0]
        end = min(item["offset"] + 12, len(item["text"]))
        # A result depends on preceding context: comparing only IDs/counts cannot pass.
        score = sum(ord(char) for row in [*context, item] for char in row["text"]) % 97 / 97
        return {"consumed": [{"start": item["offset"], "end": end,
                              "complete": end == len(item["text"])}],
                "result": {"emotion": [{"label": "平静", "probability": 1}],
                           "intent": [], "intentBroad": [], "score": score}, "durationMs": 0}

    def analyze(self, _session, context, target, **_kwargs):
        return {"analysisVersion": VERSION, "labelSchema": FINE_LABEL_SCHEMA, "groundedIntent": None,
                "emotion": [{"label": "平静", "probability": 1}],
                "intent": [{"label": "分享", "probability": 1}],
                "emotionLabel": "平静", "intentLabel": "分享", "emotionP": 1, "intentP": 1,
                "score": .5}


class RevisionTests(Library):
    def setUp(self):
        super().setUp()
        # These tests bound batch/rebuild work, not the one-time dictionary load.
        # Use the real tokenizer before starting the 12-second job wait.
        from profile_signals import keyword_counts
        keyword_counts(["合成测试初始化"])
        self.analyzer = Analyzer()
        self.results = ResultStore(self.root / "results.sqlite3")
        self.backend = Backend(self.source, analyzer=self.analyzer,
            store_factory=lambda *_: self.results,
            model_source_store=ModelSourceStore(self.root / "model.json", root=self.root),
            selection_store=ConversationSelectionStore(self.root / "selection"))
        self.addCleanup(self.backend.shutdown)

    def run_job(self, *, mode="incremental", expected="done"):
        job = self.backend.start(CONV, mode, 80 if mode == "recent" else None, expected_account=self.account)
        self.drain()
        result = next(value for value in self.backend.jobs.values() if value["id"] == job["id"])
        self.assertEqual(result["status"], expected, result)
        return result

    def drain(self):
        with self.backend.tasks.all_tasks_done:
            self.assertTrue(self.backend.tasks.all_tasks_done.wait_for(
                lambda: self.backend.tasks.unfinished_tasks == 0, timeout=12), "worker did not drain")

    def snapshot(self):
        return self.backend.batch_engine.snapshot(self.account, CONV, VERSION, self.results)

    def golden(self):
        fresh = ResultStore(self.root / "golden.sqlite3")
        backend = SimpleNamespace(source=self.source, analyzer=Analyzer(), jobs_lock=threading.Lock(), performance={},
            _legacy_profile_state=lambda *_: empty_state(), _take_recent_priority=lambda _: None,
            _assert_scope=lambda _: None)
        engine = BatchEngine(backend)
        list(engine.incremental(self.account, CONV, VERSION, fresh, {}, self.source.identity(), "golden"))
        return engine.snapshot(self.account, CONV, VERSION, fresh)

    def seed(self):
        self.ingest(record("late1", time_ms=3000, text="明天下午见"),
                    record("late2", time_ms=4000, text="abcdefghijklmnopqrstuvwx"))
        self.run_job()

    def test_backfill_rebuild_matches_clean_full_replay_and_retains_old_profile_until_done(self):
        self.seed()
        old = self.snapshot()
        self.ingest(record("early", time_ms=1000, text="先确认计划"))
        self.analyzer.block_next = True
        self.backend.start(CONV, "incremental", None, expected_account=self.account)
        self.assertTrue(self.analyzer.entered.wait(3))
        try:
            self.assertEqual(self.snapshot(), old)
            self.assertTrue(self.backend.analysis(CONV)["stale"])
            profile = self.backend.profile(CONV)
            self.assertTrue(profile["stale"])
            self.assertEqual(profile["stats"]["analyzedCount"], old["state"]["count"])
        finally:
            self.analyzer.release.set()
        self.drain()
        self.assertEqual(self.snapshot()["state"], self.golden()["state"])
        self.assertFalse(self.backend.analysis(CONV)["stale"])

    def test_recall_removes_old_contribution_and_keeps_a_tombstone(self):
        self.seed()
        self.ingest(record("late1", time_ms=3000, text="明天下午见", status="recalled", recall_time="5"))
        self.run_job()
        saved = self.snapshot()
        self.assertEqual(saved["state"]["count"], 1)
        self.assertEqual(saved["state"], self.golden()["state"])
        self.assertEqual(self.source.messages(CONV, 10)[0]["text"], "[消息已撤回]")

    def test_inflight_old_revision_cannot_commit_after_a_new_backfill(self):
        self.seed()
        old = self.snapshot()
        self.ingest(record("early", time_ms=1000))
        self.analyzer.block_next = True
        queued = self.backend.start(CONV, "incremental", None)
        self.assertTrue(self.analyzer.entered.wait(3))
        self.ingest(record("earlier", time_ms=500))
        self.analyzer.release.set()
        self.drain()
        job = next(value for value in self.backend.jobs.values() if value["id"] == queued["id"])
        self.assertEqual(job["status"], "error")
        self.assertEqual(self.snapshot(), old)
        self.run_job()
        self.assertEqual(self.snapshot()["state"], self.golden()["state"])

    def test_failed_rebuild_resumes_durable_fragments_with_a_fresh_repository_and_engine(self):
        self.seed()
        old = self.snapshot()
        self.ingest(record("early", time_ms=1000, text="a" * 30))
        self.analyzer.fail_at = len(self.analyzer.calls) + 2
        self.run_job(expected="error")
        self.assertEqual(self.snapshot(), old)
        self.analyzer.fail_at = None
        # Drop all in-memory repository/iterator state, keeping only on-disk state.
        self.backend.qq_analysis_stores.clear()
        self.backend.batch_engine = BatchEngine(self.backend)
        start = len(self.analyzer.calls)
        self.run_job()
        self.assertEqual(self.analyzer.calls[start][0][0]["offset"], 12)
        self.assertEqual(self.snapshot()["state"], self.golden()["state"])

    def test_publication_failure_rolls_back_every_table_and_can_retry_without_inference(self):
        self.seed()
        old = self.snapshot()
        self.ingest(record("early", time_ms=1000))
        with self.results.connect() as conn:
            conn.execute("CREATE TRIGGER reject_publish BEFORE INSERT ON batch_progress_v1 "
                         "BEGIN SELECT RAISE(ABORT,'synthetic publication failure'); END")
        self.run_job(expected="error")
        self.assertEqual(self.snapshot(), old)
        self.assertTrue(self.backend.analysis(CONV)["stale"])
        calls = len(self.analyzer.calls)
        with self.results.connect() as conn:
            conn.execute("DROP TRIGGER reject_publish")
        self.run_job()
        self.assertEqual(len(self.analyzer.calls), calls)
        self.assertEqual(self.snapshot()["state"], self.golden()["state"])

    def test_recent_request_after_backfill_also_rebuilds_the_complete_portrait(self):
        self.seed()
        self.ingest(record("early", time_ms=1000))
        self.run_job(mode="recent")
        self.assertEqual(self.snapshot()["state"], self.golden()["state"])
        self.assertFalse(self.backend.analysis(CONV)["stale"])

    def test_clear_revokes_and_removes_staging_without_resurrecting_old_results(self):
        self.seed()
        account, workdir, _ = self.backend._scoped_identity()
        repository = self.backend._qq_analysis_store(account, workdir, self.results)
        old_stage = repository.bind(CONV, VERSION)
        self.backend.analysis_cache_clear(self.account, LOCAL_SOURCE_ID)
        self.assertEqual(list(repository.directory.iterdir()), [])
        with self.assertRaises(StaleAnalysisError):
            old_stage.publish(complete=True)
        calls = len(self.analyzer.calls)
        self.run_job()
        self.assertGreater(len(self.analyzer.calls), calls)
        self.assertEqual(self.snapshot()["state"], self.golden()["state"])

    def test_quiesced_profile_does_not_show_the_published_snapshot(self):
        self.seed()
        self.results.suspend_cache(self.account, LOCAL_SOURCE_ID)
        profile = self.backend.profile(CONV)
        self.assertEqual(profile["job"]["status"], "suspended")
        self.assertEqual(profile["stats"]["analyzedCount"], 0)

    def test_restore_always_advances_revision_and_revokes_inflight_basis(self):
        self.seed()
        before = self.store.revision(self.account, CONV)[0]
        repository = self.backend._qq_analysis_store(*self.backend._scoped_identity()[:2], self.results)
        stage = repository.bind(CONV, VERSION)
        cursor = self.source.messages(CONV, 10)[0]["historyCursor"]
        backup = self.store.backup_to(self.root / "backup.sqlite3")
        self.store.restore_from(backup)
        self.assertGreater(self.store.revision(self.account, CONV)[0], before)
        with self.assertRaises(StaleAnalysisError):
            stage.publish(complete=True)
        with self.assertRaisesRegex(ValueError, "stale-history-cursor"):
            self.backend.history(self.account, CONV, before=cursor)

    def test_prior_revision_error_does_not_poison_new_revision_job_state(self):
        self.seed()
        self.ingest(record("early", time_ms=1000))
        self.analyzer.fail_at = len(self.analyzer.calls) + 1
        self.run_job(expected="error")
        self.assertEqual(self.backend.analysis(CONV)["job"]["status"], "error")
        self.ingest(record("earlier", time_ms=500))
        self.assertEqual(self.backend.analysis(CONV)["job"]["status"], "idle")
        self.analyzer.fail_at = None
        self.run_job()
        self.assertFalse(self.backend.analysis(CONV)["stale"])

    def test_write_guard_holds_revision_through_commit_then_rejects_late_publication(self):
        self.seed()
        repository = self.backend._qq_analysis_store(*self.backend._scoped_identity()[:2], self.results)
        stage = repository.bind(CONV, VERSION)
        entered, finished = threading.Event(), threading.Event()
        errors = []
        def ingest():
            entered.set()
            try:
                self.ingest(record("early", time_ms=1000))
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with stage.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO analysis_skips VALUES (?,?,?,?,?,?,?,?)",
                (self.account, CONV, "synthetic", VERSION, 1, "qq:" + CONV, 1, "test"))
            worker = threading.Thread(target=ingest)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(.05), "revision changed inside a guarded commit")
        self.assertTrue(finished.wait(3))
        worker.join(1)
        self.assertEqual(errors, [])
        with self.assertRaises(StaleAnalysisError):
            stage.publish(complete=True)

    def test_publication_preserves_other_scopes_and_remaps_legacy_rowid_boundaries(self):
        self.ingest(record("1", time_ms=1000), record("2", time_ms=2000))
        repository = self.backend._qq_analysis_store(*self.backend._scoped_identity()[:2], self.results)
        stage = repository.bind(CONV, VERSION)
        rows = self.source.messages(CONV, 10)
        result = {"emotion": [{"label": "平静", "probability": 1}], "intent": [], "score": .5}
        self.results.save(self.account, "u:other", "other-version", rows[0], result)
        stage.save(self.account, CONV, VERSION, rows[0], result)
        batches = self.backend.batch_engine.store(stage)
        batches.seed(self.account, CONV, VERSION, CONV, empty_state(), None, [])
        stage.save(self.account, CONV, VERSION, rows[1], result)
        stage.publish(complete=True)
        self.assertEqual(self.results.ids(self.account, "u:other", "other-version"), {"1"})
        published = self.backend.batch_engine.store(self.results)
        self.assertEqual(published.legacy_known(self.account, CONV, VERSION, CONV, ["1", "2"]), {"1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

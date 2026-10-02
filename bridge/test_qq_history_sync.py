"""History work and late-message repair on the actual coordinator, synthetic only."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_qq_sync as sync_fixture
from qq_sync import QQSync
from qq_connector import ConnectorError
from qq_sync_work import initial, advance, validate
from qq_message_store import QQMessageStore
from test_qq_message_store import CONV, record

BASE = sync_fixture.BASE


class FixtureLifecycleTests(unittest.TestCase):
    def test_failed_nested_connect_closes_the_library_before_removing_its_temp_root(self):
        fixture = HistoryTests()
        try:
            with patch.object(QQSync, "connect", side_effect=ConnectorError("connection-unavailable")):
                with self.assertRaises(ConnectorError):
                    fixture.setUp()
            root = fixture.fixture.fixture.root
            source = fixture.fixture.source
        finally:
            fixture.doCleanups()
        self.assertFalse(root.exists())
        self.assertTrue(source._closed)


class PlanTests(unittest.TestCase):
    def test_empty_partial_never_advances_coverage(self):
        state = initial("history", 100000)
        changed = advance("history", state, "partial", "page-repeated", 200000)
        self.assertEqual(changed["coverageStartMs"], 100000)
        self.assertEqual(changed["window"], [0, 100000])

    def test_budget_splits_newest_half_first_without_losing_older_work(self):
        state = initial("history", 100000)
        for _ in range(3):
            state = advance("history", state, "partial", "budget-exhausted", 200000)
        self.assertEqual(state["window"], [50000, 100000])
        self.assertEqual(state["stack"], [[0, 50000]])
        self.assertEqual(state["coverageStartMs"], 100000)
        newer = advance("history", state, "complete", None, 200000)
        self.assertEqual((newer["window"], newer["coverageStartMs"]), ([0, 50000], 50000))
        done = advance("history", newer, "complete-empty", None, 200001)
        self.assertEqual((done["window"], done["coverageStartMs"], done["state"]), (None, 0, "complete"))

    def test_dense_single_second_stays_partial_instead_of_claiming_complete(self):
        state = initial("history", 1000)
        for _ in range(3):
            state = advance("history", state, "partial", "budget-exhausted", 2000)
        self.assertEqual(state["state"], "partial")
        self.assertEqual(state["window"], [0, 1000])

    def test_gapped_or_reordered_stack_is_rejected(self):
        state = initial("history", 100000)
        state.update(window=[60000, 100000], stack=[[0, 50000]])
        with self.assertRaisesRegex(ValueError, "sync-work-gap"):
            validate("history", state)

    def test_reconciliation_completes_only_its_recent_interval(self):
        state = initial("reconcile", 100000, 90000)
        state = advance("reconcile", state, "complete", None, 100001)
        self.assertEqual((state["coverageStartMs"], state["lastWindow"]), (90000, [90000, 100000]))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sync_fixture.SyncTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.sync, self.source = self.fixture.sync, self.fixture.source
        self.account, self.server = self.fixture.account, self.fixture.server
        self.sync.run_once(CONV)
        self.tail = dict(self.fixture.checkpoint())
        self.clock = [self.sync.monotonic()]
        self.sync.monotonic = lambda: self.clock[0]

    def work(self, kind="history"):
        with self.source.read_library(self.account) as (_, library):
            return library.sync_work(self.account, CONV, kind)

    def old(self, count=10):
        for index in range(count):
            row = copy.deepcopy(self.server.server.dataset[-1])
            row.update(msgId=str(9007199254790000 + index),
                       msgTime=str(sync_fixture.BASE_EPOCH_S - 3600 + index), text=f"旧历史合成消息{index}")
            self.server.server.dataset.append(row)

    def finish(self, limit=300):
        for _ in range(limit):
            self.sync.run_once(CONV, work_kind="history")
            state = self.work()
            if state and state["payload"]["reason"] == "request-budget-exhausted":
                self.clock[0] += 61
                continue
            if state and state["payload"]["state"] == "complete":
                return state
            if state and state["payload"]["attempts"] >= 3:
                self.fail("unexpected parked history window: " + str(state["payload"]["reason"]))
        self.fail("history did not complete within synthetic bound")

    def test_old_history_and_work_checkpoint_commit_without_changing_tail_frontier(self):
        self.old()
        done = self.finish()
        self.assertEqual(done["payload"]["coverageStartMs"], 0)
        self.assertEqual(self.fixture.checkpoint(), self.tail)
        self.assertEqual(sum(item["text"].startswith("旧历史") for item in self.source.messages(CONV, 100)), 10)

    def test_partial_history_is_replayed_after_process_restart_from_page_one(self):
        self.old(30)
        self.sync.run_once(CONV, work_kind="history")
        previous = self.work()
        self.assertEqual(previous["payload"]["state"], "partial")
        self.sync.close()
        restarted = QQSync(self.fixture.api, live_validated=True, config_store=self.fixture.config,
            policy=self.sync.policy, autostart=False, wall_ms=lambda: BASE + 30000)
        self.addCleanup(restarted.close)
        self.fixture.api.sync_manager = restarted
        self.fixture.backend.qq_sync = restarted
        self.sync = restarted
        before = len(self.server.requests)
        restarted.run_once(CONV, work_kind="history")
        fetch = next(item for item in self.server.requests[before:] if item["method"] == "POST")
        self.assertEqual(fetch["body"]["page"], 1)
        self.assertEqual(fetch["body"]["filter"], {"startTime": previous["payload"]["window"][0], "endTime": previous["payload"]["window"][1]})

    def test_late_message_outside_forward_overlap_is_found_by_reconciliation(self):
        extra = copy.deepcopy(self.server.server.dataset[-1])
        extra.update(msgId="9007199254750000", msgTime=str(sync_fixture.BASE_EPOCH_S + 5), text="迟到的合成消息")
        self.server.server.dataset.append(extra)
        self.sync.run_once(CONV, now_ms=BASE + 40000)
        self.assertFalse(any(item["text"] == "迟到的合成消息" for item in self.source.messages(CONV, 100)))
        tail = dict(self.fixture.checkpoint())
        result = self.sync.run_once(CONV, now_ms=BASE + 41000, work_kind="reconcile")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.fixture.checkpoint(), tail)
        self.assertTrue(any(item["text"] == "迟到的合成消息" for item in self.source.messages(CONV, 100)))

    def test_dense_history_splits_and_eventually_covers_every_native_id(self):
        self.old(100)
        done = self.finish()
        self.assertEqual(done["payload"]["coverageStartMs"], 0)
        with self.source.read_library(self.account) as (_, library):
            ids = [row[0] for row in library.connection.execute("SELECT native_id FROM messages WHERE native_id LIKE '900719925479%'")]
        self.assertEqual(len(set(ids)), 100)

    def test_work_publish_failure_rolls_back_messages_and_progress(self):
        self.old()
        with patch("qq_sync_work.write", side_effect=ValueError("synthetic publish failure")):
            with self.assertRaisesRegex(ValueError, "synthetic publish failure"):
                self.sync.run_once(CONV, work_kind="history")
        self.assertIsNone(self.work())
        self.assertFalse(any(item["text"].startswith("旧历史") for item in self.source.messages(CONV, 100)))
        self.assertEqual(self.fixture.checkpoint(), self.tail)

    def test_stale_work_revision_rejects_late_message_commit(self):
        self.old()
        self.sync.run_once(CONV, work_kind="history")
        state = self.work()
        with self.source.read_library(self.account) as (_, library):
            before = library.counts(self.account, CONV)
            with self.assertRaisesRegex(ValueError, "stale-sync-work"):
                library.ingest(self.account, CONV, [record("late-stale")],
                    sync_work=("history", state["revision"] - 1, state["payload"]))
            self.assertEqual(library.counts(self.account, CONV), before)

    def test_restoring_backup_clears_coverage_instead_of_reusing_newer_progress(self):
        self.old()
        self.finish()
        with self.source.read_library(self.account) as (_, library):
            backup = library.path.with_name("temporary-backup.sqlite")
            library.backup_to(backup)
            library.restore_from(backup)
            self.assertIsNone(library.sync_work(self.account, CONV, "history"))

    def test_unselected_history_is_not_requested(self):
        self.fixture.backend.set_conversation_selected(self.account, CONV, False)
        before = len(self.server.requests)
        with self.assertRaisesRegex(Exception, "conversation-not-selected"):
            self.sync.run_once(CONV, work_kind="history")
        self.assertEqual(len(self.server.requests), before)

    def test_history_scan_does_not_replace_the_forward_window_display(self):
        before = copy.deepcopy(self.sync.public()["conversations"][CONV])
        self.old()
        self.sync.run_once(CONV, work_kind="history")
        after = self.sync.public()["conversations"][CONV]
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertIn("history", after)

    def test_reconnect_reloads_durable_history_instead_of_retaining_parked_deadline(self):
        self.old(30)
        self.sync.run_once(CONV, work_kind="history")
        previous = copy.deepcopy(self.work())
        self.sync.work_due[(self.account, CONV, "history")] = float("inf")
        self.sync.disconnect()
        self.sync.connect()
        self.assertEqual(self.sync.work_due, {})
        self.assertEqual(self.work(), previous)
        self.sync.run_once(CONV)
        self.assertEqual(self.sync.public()["conversations"][CONV]["history"]["window"], previous["payload"]["window"])

    def test_restore_then_forward_refresh_removes_stale_complete_history(self):
        self.old()
        self.finish()
        with self.source.read_library(self.account) as (_, library):
            backup = library.path.with_name("temporary-backup.sqlite")
            library.backup_to(backup)
            library.restore_from(backup)
        self.sync.run_once(CONV)
        self.assertNotIn("history", self.sync.public()["conversations"][CONV])
        self.assertNotIn((self.account, CONV, "history"), self.sync.work_due)
        self.finish()

    def test_deselection_at_history_commit_rolls_back_work_and_new_rows(self):
        self.old()
        original = QQMessageStore.ingest
        def deselect(library, *values, **options):
            if options.get("sync_work"):
                self.fixture.backend.set_conversation_selected(self.account, CONV, False)
            return original(library, *values, **options)
        with patch.object(QQMessageStore, "ingest", deselect):
            self.assertEqual(self.sync.run_once(CONV, work_kind="history")["state"], "cancelled")
        self.assertIsNone(self.work())
        self.assertFalse(any(item["text"].startswith("旧历史") for item in self.source.messages(CONV, 100)))
        self.assertNotIn(CONV, self.sync.public()["conversations"])

    def test_a_to_b_to_a_during_history_never_publishes_rows_or_work(self):
        self.old()
        original = self.sync.connector.scan
        def switch(*values, **options):
            result = original(*values, **options)
            self.source.attach("10003")
            self.source.attach("10001")
            return result
        with patch.object(self.sync.connector, "scan", switch):
            self.assertEqual(self.sync.run_once(CONV, work_kind="history")["state"], "cancelled")
        self.assertIsNone(self.work())
        self.assertEqual(self.fixture.checkpoint(), self.tail)

    def test_global_quota_wait_keeps_history_retry_allowance(self):
        self.old()
        while len(self.sync.request_times) < self.sync.policy.requests_per_minute:
            self.sync._charge_request()
        result = self.sync.run_once(CONV, work_kind="history")
        self.assertEqual(result["reason"], "request-budget-exhausted")
        self.assertEqual(self.work()["payload"]["attempts"], 0)
        self.assertGreater(self.sync.work_due[(self.account, CONV, "history")], self.clock[0])
        self.clock[0] += 61
        self.sync.run_once(CONV, work_kind="history")
        self.assertNotEqual(self.work()["payload"]["reason"], "request-budget-exhausted")

    def test_real_worker_prioritizes_due_tail_before_history_and_reconcile(self):
        calls = []
        self.sync.due.clear()
        def run(user, **options):
            calls.append(options.get("work_kind", "tail"))
            self.sync.closed = True
        with patch.object(self.sync, "run_once", run):
            self.sync._loop()
        self.assertEqual(calls, ["tail"])
        self.sync.closed = False

    def test_background_connection_failure_does_not_report_connected(self):
        with patch.object(self.sync.connector, "scan", side_effect=ConnectorError("connection-unavailable")):
            result = self.sync.run_once(CONV, work_kind="history")
        self.assertEqual(result["state"], "error")
        self.assertEqual(self.sync.public()["state"], "offline")
        self.assertEqual(self.sync.public()["reason"], "connection-unavailable")
        self.assertEqual(self.work()["payload"]["attempts"], 1)
        self.assertEqual(self.fixture.checkpoint(), self.tail)

    def test_real_worker_runs_history_when_tail_and_reconcile_are_not_due(self):
        calls = []
        self.sync.due[(self.account, CONV)] = float("inf")
        self.sync.work_due[(self.account, CONV, "reconcile")] = float("inf")
        def run(user, **options):
            calls.append(options.get("work_kind", "tail"))
            self.sync.closed = True
        with patch.object(self.sync, "run_once", run):
            self.sync._loop()
        self.assertEqual(calls, ["history"])
        self.sync.closed = False


if __name__ == "__main__":
    unittest.main()

"""Synthetic API portrait backfill, source scope, and checkpoint transaction checks."""
from copy import deepcopy
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch

from api_portrait_ledger import ApiPortraitLedger, scope as ledger_scope
from api_portrait_statistics import valid_statistics
from portrait_contracts import api_portrait_scope
from backend_contracts import empty_api_portrait
import test_api_insights as api_fixtures


class ApiPortraitReconciliationTests(unittest.TestCase):
    activate = api_fixtures.ApiInsightTests.activate
    wait_portrait = api_fixtures.ApiInsightTests.wait_portrait
    wait_portrait_inventory = api_fixtures.ApiInsightTests.wait_portrait_inventory

    def setUp(self):
        api_fixtures.ApiInsightTests.setUp(self)
        self.source.history_revision = lambda user: self.signature(self.source.rows)
        self.source.history_prefix_signature = lambda user, ceiling: self.signature(
            [row for row in self.source.rows if tuple(row["_sort"]) <= tuple(ceiling)])
        lexical = patch("api_portrait_ledger.keyword_counts", return_value={"synthetic": 1})
        lexical.start()
        self.addCleanup(lexical.stop)
        self.selected = self.activate()

    @staticmethod
    def signature(rows):
        facts = [(row["id"], row["_sort"], len(row.get("text", "")), row["kind"])
                 for row in rows]
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()

    def insert(self, stable_id, position, text="synthetic old message", sender="friend"):
        self.source.rows.append({"id": stable_id, "side": "other", "kind": "text", "text": text,
                                 "senderId": sender, "_sort": list(position)})
        self.source.rows.sort(key=lambda row: tuple(row["_sort"]))

    def repository(self):
        return self.backend.store_factory("account-a", self.source.workdir)

    def ledger(self, user="friend", subject=None, selected=None):
        source_id = (selected or self.selected)["sourceId"]
        adapter = ApiPortraitLedger(self.repository())
        identity = ledger_scope("account-a", user, api_portrait_scope(source_id), subject or user,
                                self.analyzer.portrait_version())
        return adapter, identity

    def saved(self, user="friend", subject=None, selected=None):
        return self.repository().api_portrait_get("account-a", user,
                                                  api_portrait_scope((selected or self.selected)["sourceId"]),
                                                  subject or user)

    def wait_for(self, user, member=None):
        if user == "friend" and member is None:
            return self.wait_portrait()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.backend.model_portrait(user, member)
            if result["job"]["status"] in ("done", "error"):
                return result
            time.sleep(.01)
        self.fail("synthetic API portrait job did not finish")

    def run_portrait(self, user="friend", member=None):
        self.backend.start_model_portrait("account-a", user, member)
        result = self.wait_for(user, member)
        self.assertEqual(result["job"]["status"], "done", result["job"])
        return result

    def target_ids(self, start=0):
        return [row["messageId"] for _model, _previous, batch in self.analyzer.portrait_calls[start:]
                for row in batch if row["target"]]

    def test_same_highwater_old_insert_classifies_only_missing_id_in_chronology(self):
        self.run_portrait()
        ledger, identity = self.ledger()
        self.assertEqual(ledger.known_ids(identity), {"s1", "o1", "o2"})
        self.assertTrue(ledger.metadata(identity)["ready"])
        before = len(self.analyzer.portrait_calls)
        original = self.analyzer.classify_portrait_batch

        def distinct_signal(*args):
            result = original(*args)
            result["result"]["score"] = .1
            return result

        self.analyzer.classify_portrait_batch = distinct_signal
        highwater = self.source.history_highwater("friend")
        self.insert("old", (0, "old-shard", 1))
        self.assertEqual(self.source.history_highwater("friend"), highwater)
        done = self.run_portrait()
        self.assertEqual(self.target_ids(before), ["old"])
        statistics = self.saved()["resume"]["portraitStatistics"]["state"]
        self.assertEqual(statistics["targetCount"], 3)
        self.assertAlmostEqual(statistics["scoreSum"], 1.2)
        self.assertAlmostEqual(statistics["scoreWeighted"], 1.65)
        self.assertEqual(statistics["moodCount"], 3)
        self.assertEqual(statistics["mood"]["happy"]["weighted"], 3.0)
        self.assertEqual(done["nativeProfile"]["portraitCount"], 3)

    def test_unrelated_source_revision_only_checks_the_prefix_once(self):
        self.run_portrait()
        signature = self.source.history_revision
        self.source.history_revision = lambda user: signature(user) + ":unrelated-new-message"
        original = self.source.history_prefix_signature
        with patch.object(self.source, "history_prefix_signature", wraps=original) as query:
            for _index in range(5):
                self.assertTrue(self.backend.model_portrait("friend")["progress"]["complete"])
            self.assertEqual(query.call_count, 1)

    def test_old_insert_plus_tail_insert_repeat_has_zero_provider_calls_or_pages(self):
        self.run_portrait()
        before = len(self.analyzer.portrait_calls)
        self.insert("old", (0, "old-shard", 1))
        self.insert("tail", (4, "new-shard", 1))
        self.run_portrait()
        self.assertEqual(self.target_ids(before), ["old", "tail"])
        state = self.saved()["resume"]["portraitStatistics"]["state"]
        self.assertEqual((state["count"], state["targetCount"]), (4, 4))
        self.assertAlmostEqual(state["scoreWeighted"], 3.3)
        before, pages = len(self.analyzer.portrait_calls), self.source.history_pages
        self.run_portrait()
        self.assertEqual(len(self.analyzer.portrait_calls), before)
        self.assertEqual(self.source.history_pages, pages)
        ledger, identity = self.ledger()
        self.assertEqual(ledger.known_ids(identity), {"s1", "o1", "o2", "old", "tail"})

    def test_old_long_message_fault_resume_preserves_pending_before_latest(self):
        self.activate(context_tokens=12288)
        self.run_portrait()
        before = len(self.analyzer.portrait_calls)
        self.insert("old-long", (0, "old-shard", 1), text="synthetic old long text " * 1200)
        self.analyzer.fail_portrait_at = before + 2
        self.backend.start_model_portrait("account-a", "friend")
        failed = self.wait_portrait()
        self.assertEqual(failed["job"]["status"], "error")
        saved = self.saved()
        self.assertEqual(saved["batchIndex"], 1)
        statistics = saved["resume"]["portraitStatistics"]
        self.assertEqual(statistics["pending"]["messageId"], "old-long")
        self.assertLess(tuple(statistics["pending"]["position"]), tuple(statistics["state"]["latest"][:3]))
        self.assertTrue(valid_statistics(statistics, allow_historical_pending=True))
        ledger, identity = self.ledger()
        self.assertFalse(ledger.metadata(identity)["ready"])
        self.run_portrait()
        saved = self.saved()
        self.assertIsNone(saved["resume"]["portraitStatistics"]["pending"])
        self.assertEqual(saved["resume"]["portraitStatistics"]["state"]["targetCount"], 3)
        self.assertAlmostEqual(saved["resume"]["portraitStatistics"]["state"]["scoreWeighted"], 1.65)
        self.assertEqual(set(self.target_ids(before)), {"old-long"})
        self.assertEqual(self.analyzer.portrait_calls[before+2][2],
                         self.analyzer.portrait_calls[before+1][2], "resume must replay the first uncommitted fragment")
        first_piece = [row["id"] for _model, _previous, batch in self.analyzer.portrait_calls[before:]
                       for row in batch if row["target"] and row["id"] == "old-long:0"]
        self.assertEqual(first_piece, ["old-long:0"], "committed first fragment must not incur another call")
        self.assertEqual(ledger.known_ids(identity), {"s1", "o1", "o2", "old-long"})
        self.assertTrue(ledger.metadata(identity)["ready"])
        calls, pages = len(self.analyzer.portrait_calls), self.source.history_pages
        self.run_portrait()
        self.assertEqual((len(self.analyzer.portrait_calls), self.source.history_pages), (calls, pages))

    def test_legacy_without_ledger_rebuilds_once_and_keeps_display_during_task(self):
        self.run_portrait()
        ledger, identity = self.ledger()
        old = empty_api_portrait()
        old["summary"] = "previous synthetic portrait"
        with self.repository().connect() as connection:
            ledger.clear(connection, identity)
            row = connection.execute("SELECT resume_json FROM api_portrait_v1").fetchone()
            resume = json.loads(row[0])
            resume.pop("portraitLedgerVersion", None)
            connection.execute("UPDATE api_portrait_v1 SET portrait_json=?,resume_json=?",
                               (json.dumps(old), json.dumps(resume)))
        self.assertEqual(self.backend.model_portrait("friend")["portrait"], old)
        previous_count = self.backend.model_portrait("friend")["nativeProfile"]["portraitCount"]
        before = len(self.analyzer.portrait_calls)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.analyzer.classify_portrait_batch

        def blocked(*args):
            entered.set()
            release.wait(timeout=3)
            return original(*args)

        self.analyzer.classify_portrait_batch = blocked
        self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(self.backend.model_portrait("friend")["portrait"], old)
        during = self.backend.model_portrait("friend")
        self.assertEqual(during["nativeProfile"]["portraitCount"], previous_count)
        self.assertTrue(during["rebuilding"])
        release.set()
        done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done", done["job"])
        self.assertEqual(self.target_ids(before), ["o1", "o2"])
        self.assertTrue(ledger.metadata(identity)["ready"])
        before = len(self.analyzer.portrait_calls)
        self.run_portrait()
        self.assertEqual(len(self.analyzer.portrait_calls), before)

    def test_member_and_group_reset_preserve_other_members_sources_and_accounts(self):
        room = "room@chatroom"
        self.insert("other-member", (4, "shard", 4), sender="other-friend")
        self.run_portrait(room)
        self.run_portrait(room, "friend")
        self.run_portrait(room, "other-friend")
        first = self.selected
        self.selected = self.activate(model="other-source")
        self.run_portrait(room)
        second = self.selected
        self.backend.analysis_scope_clear("account-a", room, first["sourceId"], "portrait", "friend")
        self.assertIsNone(self.saved(room, "friend", first))
        friend_ledger, friend_scope = self.ledger(room, "friend", first)
        self.assertEqual(friend_ledger.known_ids(friend_scope), set())
        self.assertIsNotNone(self.saved(room, "other-friend", first))
        self.assertIsNotNone(self.saved(room, selected=first))
        self.assertIsNotNone(self.saved(room, selected=second))
        self.backend.analysis_scope_clear("account-a", room, first["sourceId"], "portrait")
        self.assertIsNone(self.saved(room, selected=first))
        self.assertIsNotNone(self.saved(room, "other-friend", first))
        self.assertIsNotNone(self.saved(room, selected=second))
        other_scope = ledger_scope("other-account", room, api_portrait_scope(first["sourceId"]), room,
                                   self.analyzer.portrait_version())
        adapter = ApiPortraitLedger(self.repository())
        with self.repository().connect() as connection:
            adapter.begin(connection, other_scope)
        self.backend.analysis_scope_clear("account-a", room, first["sourceId"], "conversation")
        self.assertIsNone(self.saved(room, "other-friend", first))
        self.assertIsNotNone(self.saved(room, selected=second))
        self.assertTrue(adapter.metadata(other_scope)["initialized"])

    def test_ledger_callback_failure_rolls_back_portrait_and_ledger_atomically(self):
        self.run_portrait()
        repository = self.repository()
        ledger, identity = self.ledger()
        previous = self.saved()
        metadata = ledger.metadata(identity)
        records = ledger.records(identity)
        piece = {"messageId": "rollback-old", "speaker": "friend", "sender": "OTHER", "target": True,
                 "text": "synthetic rollback old", "_sort": [0, "old-shard", 1], "_pieceIndex": 0, "_last": True}
        prepared = ledger.prepare(previous["resume"]["portraitStatistics"], self.analyzer.portrait_signal(),
                                  [piece], local_subject="friend", selected=identity)
        statistics = ledger.statistics(identity, prepared)
        resume = deepcopy(previous["resume"])
        resume["portraitStatistics"] = statistics

        def failing_callback(connection):
            ledger.persist(connection, identity, prepared, revision="uncommitted-revision", ready=False)
            raise RuntimeError("synthetic ledger checkpoint failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic ledger checkpoint failure"):
            repository.api_portrait_checkpoint("account-a", "friend", identity.source_id, "friend",
                                               previous["batchIndex"], previous["portrait"], previous["processed"],
                                               previous["processedChars"], False, resume=resume,
                                               ledger_update=failing_callback)
        self.assertEqual(self.saved(), previous)
        self.assertEqual(ledger.metadata(identity), metadata)
        self.assertEqual(ledger.records(identity), records)
        self.assertNotIn("rollback-old", ledger.known_ids(identity))


if __name__ == "__main__":
    unittest.main()

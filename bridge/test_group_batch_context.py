"""Group context survives batching and SQLite resume; synthetic data only."""
import copy
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from batch_engine import BatchEngine
from node_analysis import NodeAnalysis
from profile_state import empty_state
from qq_normalize import normalize_export
from qq_roles import group_context
from result_store import ResultStore
from test_qq_analysis_revision import Analyzer, VERSION
from test_qq_groups import PEOPLE, export_document
from test_qq_local_integrity import Library


def group_message():
    return {"id": "synthetic-target", "kind": "text", "side": "other",
            "text": "合成群聊内容", "conversationKind": "group",
            "senderId": "uin:10002", "time": 1000,
            "quote": {"senderId": "uin:10003", "text": "合成引用内容"},
            "mentions": [{"senderId": "uin:10001", "kind": "user"}]}


class ContextProjectionTests(unittest.TestCase):
    def test_group_context_is_idempotent_and_survives_json_roundtrip(self):
        first = BatchEngine.context([group_message()])
        self.assertEqual(BatchEngine.context(first), first)
        self.assertEqual(BatchEngine.context(json.loads(json.dumps(first))), first)
        self.assertEqual(first[0]["conversationKind"], "group")

    def test_legacy_compact_snapshot_keeps_speaker_quote_and_mentions(self):
        raw = group_message()
        role = group_context(raw)
        legacy = {"id": raw["id"], "side": raw["side"], "text": raw["text"],
                  "groupContext": role}
        result = BatchEngine.context([legacy])
        self.assertEqual(result[0]["groupContext"], role)
        self.assertEqual(result[0]["conversationKind"], "group")
        result[0]["groupContext"]["mentions"].clear()
        self.assertEqual(len(legacy["groupContext"]["mentions"]), 1)

    def test_explicit_invalid_group_context_is_not_silently_removed(self):
        raw = {"id": "invalid", "side": "other", "text": "合成内容",
               "conversationKind": "group", "groupContext": None}
        self.assertIn("groupContext", BatchEngine.context([raw])[0])

    def test_friend_context_keeps_its_existing_three_text_message_contract(self):
        rows = [{"id": str(i), "side": "other", "text": str(i),
                 "kind": "text", "conversationKind": "friend"} for i in range(4)]
        rows.append({"id": "image", "side": "other", "text": "", "kind": "image"})
        expected = [{"id": str(i), "side": "other", "text": str(i)} for i in range(1, 4)]
        self.assertEqual(BatchEngine.context(rows), expected)


class GroupBatchResumeTests(unittest.TestCase):
    def setUp(self):
        self.library = Library()
        self.library.setUp()
        self.addCleanup(self.library.doCleanups)
        self.account = self.library.account
        self.user = "g:20001"
        records = normalize_export(export_document(), expected_kind="group")["records"]
        self.library.store.ensure_conversation(self.account, self.user, kind="group", group_code="20001")
        self.library.store.ingest(self.account, self.user, records)
        self.library.store.set_members(self.account, self.user, PEOPLE)
        self.results = ResultStore(self.library.root / "group-results.sqlite3")
        self.calls = []
        self.analyzer = Analyzer()

        def strict_batch(session, messages, context):
            for row in context:
                role = row.get("groupContext")
                if not isinstance(role, dict) or not role.get("speaker"):
                    raise RuntimeError("group context required")
            self.calls.append(copy.deepcopy((messages, context)))
            # One record per call forces context reuse even for a small group.
            response = self.analyzer.analyze_batch(session, messages[:1], context)
            response["result"]["score"] = None
            return response

        self.backend = SimpleNamespace(source=self.library.source,
            analyzer=SimpleNamespace(analyze_batch=strict_batch),
            jobs_lock=threading.Lock(), performance={},
            _legacy_profile_state=lambda *_: empty_state(),
            _take_recent_priority=lambda _: None, _assert_scope=lambda _: None,
            _scope_commit=lambda _: nullcontext())
        self.engine = BatchEngine(self.backend)
        self.scope = self.library.source.identity()
        self.key = (self.account, str(self.results.path), self.user, VERSION)

    def run_member(self, engine, results, member):
        return engine.incremental(self.account, self.user, VERSION, results, {},
                                  self.scope, self.key, member)

    def test_each_member_crosses_batches_without_losing_background_roles(self):
        for member in ("uin:10001", "uin:10002", "uin:10003"):
            self.calls.clear()
            list(self.run_member(self.engine, self.results, member))
            self.assertGreater(len(self.calls), 1)
            saved = self.engine.snapshot(self.account, self.user, VERSION, self.results, member)
            self.assertTrue(saved["complete"])
            self.assertEqual(saved["state"]["count"], 2)
            self.assertTrue(all("groupContext" in row for row in saved["context"]))
            targets = [row for messages, _ in self.calls for row in messages[:1] if row["target"]]
            self.assertEqual({row["senderId"] for row in targets}, {member})

    def test_infer_projects_raw_group_background_before_the_worker_call(self):
        member = "uin:10002"
        self.engine.ensure(self.account, self.user, VERSION, self.results, self.scope, member)
        target = next(row for row in self.library.source.messages(self.user, 20)
                      if row["senderId"] == member and row["kind"] == "text")
        self.engine.infer(self.account, self.user, VERSION, self.results, self.scope, member,
                          [target], [group_message()], advance=True)
        self.assertEqual(self.calls[0][1][0]["groupContext"], group_context(group_message()))

    def test_reopened_engine_resumes_legacy_compact_sqlite_context_without_recounting(self):
        member = "uin:10002"
        iterator = self.run_member(self.engine, self.results, member)
        next(iterator)
        iterator.close()
        saved = self.engine.snapshot(self.account, self.user, VERSION, self.results, member)
        self.assertTrue(saved["context"])
        # Exact legacy on-disk shape: groupContext exists, conversationKind does not.
        compact = [{key: value for key, value in row.items() if key != "conversationKind"}
                   for row in saved["context"]]
        self.engine.store(self.results).move_cursor(self.account, self.user, VERSION, member,
            saved["cursor"], compact, char_offset=saved["charOffset"])
        reopened_results = ResultStore(self.results.path)
        reopened = BatchEngine(self.backend)
        list(self.run_member(reopened, reopened_results, member))
        final = reopened.snapshot(self.account, self.user, VERSION, reopened_results, member)
        self.assertTrue(final["complete"])
        self.assertEqual(final["state"]["count"], 2)
        completed_targets = [messages[0]["id"] for messages, _ in self.calls
                             if messages[0]["target"] and
                             len(messages[0]["text"]) - messages[0]["offset"] <= 12]
        self.assertEqual(len(completed_targets), len(set(completed_targets)))


class GroupWorkerDiagnosticTests(unittest.TestCase):
    def request_error(self, error, message, *, group=True):
        with tempfile.TemporaryDirectory() as root:
            worker = NodeAnalysis(settings_path=Path(root) / "inference.json")
            worker.version = worker.running_version = "synthetic"
            worker.model = {"state": "ready"}
            worker.process = Mock()
            worker.process.poll.return_value = None
            worker._launch_locked = Mock()
            def reply(_text):
                worker.pending[worker.serial] = {"error": error, "message": message}
            worker.process.stdin.write.side_effect = reply
            with self.assertRaises(RuntimeError) as raised:
                worker._request({"cmd": "batch", **({"conversationKind": "group"} if group else {})})
            return str(raised.exception)

    def test_known_group_validation_reason_survives_the_worker_bridge(self):
        self.assertEqual(self.request_error("Error", "group context required"),
                         "Error: group context required")

    def test_arbitrary_worker_message_is_not_exposed(self):
        self.assertEqual(self.request_error("Error", "synthetic-private-message"), "Error")

    def test_connector_error_codes_and_friend_errors_stay_compatible(self):
        self.assertEqual(self.request_error("invalid-output", "group context required"), "invalid-output")
        self.assertEqual(self.request_error("Error", "group context required", group=False), "Error")


if __name__ == "__main__":
    unittest.main()

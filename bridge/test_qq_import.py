"""Synthetic export/HTTP regressions with isolated temporary accounts only."""
import copy
import json
import threading
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from account_store import account_id
from qq_import import ImportCancelled
from qq_message_store import QQMessageStore
from qq_normalize import read_export
from real_http import make_handler
import test_qq_accounts as accounts_fixture
from test_qq_export import GOLDEN


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = accounts_fixture.QQAccountTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.api = self.fixture.api
        self.imports = self.api.imports
        self.addCleanup(self.imports.close)
        self.path = self.fixture.root / "合成 聊天.json"
        self.document = copy.deepcopy(GOLDEN["export"])
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")

    def wait(self, job):
        self.imports.job["thread"].join(timeout=15)
        self.assertFalse(self.imports.job["thread"].is_alive())
        return self.imports.status(job["jobId"])

    def preview(self, owner=None):
        return self.wait(self.imports.start(str(self.path), owner))

    def commit(self, preview, partial=False):
        return self.wait(self.imports.commit(preview["jobId"], preview["previewToken"], partial))

    def test_preview_is_readonly_and_commit_can_create_offline_account(self):
        preview = self.preview()
        self.assertEqual(preview["state"], "ready")
        self.assertEqual(preview["preview"]["directions"]["peer"], 1)
        self.assertFalse(self.fixture.data_root.exists())
        result = self.commit(preview)
        self.assertEqual((result["state"], result["result"]["inserted"]), ("complete", 2))
        self.assertEqual(self.fixture.backend.health()["data"]["state"], "account-unavailable")
        self.assertEqual(self.api.list()["accounts"][0]["displayId"], "10001")
        self.api.activate(result["result"]["accountId"])
        messages = self.fixture.source.messages(preview["preview"]["conversationKey"], 80)
        self.assertEqual([row["side"] for row in messages], ["other", "self"])

    def test_preview_records_match_existing_normalizer_exactly(self):
        preview = self.preview()
        expected = read_export(self.path)
        with closing(__import__('sqlite3').connect(self.imports.job["database"])) as db:
            actual = [json.loads(row[0]) for row in db.execute("SELECT record FROM records ORDER BY idx")]
        self.assertEqual(actual, expected["records"])
        self.assertEqual(preview["preview"]["counts"], expected["counts"])

    def test_owner_mapping_requires_explicit_choice_and_rotates_preview_token(self):
        del self.document["chatInfo"]["selfUin"]
        self.write()
        preview = self.preview()
        self.assertEqual((preview["state"], preview["reason"]), ("identity", "owner-required"))
        mapped = self.wait(self.imports.map_owner(preview["jobId"], "0010001"))
        self.assertEqual(mapped["preview"]["ownerUin"], "10001")
        old = mapped["previewToken"]
        mapped = self.wait(self.imports.map_owner(mapped["jobId"], "10001"))
        with self.assertRaisesRegex(ValueError, "stale-import-preview"):
            self.imports.commit(mapped["jobId"], old)

    def test_explicit_owner_cannot_override_declared_identity(self):
        self.assertEqual(self.preview("10002")["error"], "owner-mismatch")

    def test_repeated_import_uses_native_ids_and_leaves_revision_unchanged(self):
        first = self.commit(self.preview())
        self.api.activate(first["result"]["accountId"])
        source = self.fixture.source
        before = source._store.revision(first["result"]["account"], first["result"]["conversationKey"])
        second = self.commit(self.preview())
        self.assertEqual((second["result"]["inserted"], second["result"]["unchanged"]), (0, 2))
        self.assertEqual(source._store.revision(first["result"]["account"], first["result"]["conversationKey"]), before)

    def test_historical_import_increments_revision_and_preserves_sync_checkpoint(self):
        first = self.commit(self.preview())
        self.api.activate(first["result"]["accountId"])
        account, conv = first["result"]["account"], first["result"]["conversationKey"]
        library = self.fixture.source._store
        old = library.revision(account, conv)
        self.document["messages"][0]["messageId"] = "9007199254740995"
        self.document["messages"][0]["timestamp"] = "2025-07-10T12:00:00.123Z"
        self.document["messages"] = self.document["messages"][:1]
        self.write()
        result = self.commit(self.preview())
        self.assertEqual(result["result"]["backfilled"], 1)
        self.assertGreater(library.revision(account, conv), old)
        self.assertEqual(library.connection.execute("SELECT COUNT(*) FROM sync_checkpoints").fetchone()[0], 0)

    def test_bad_rows_are_visible_and_need_partial_confirmation(self):
        self.document["messages"].append({"broken": True})
        self.write()
        preview = self.preview()
        self.assertEqual(preview["preview"]["counts"]["rowsRejected"], 1)
        with self.assertRaisesRegex(ValueError, "partial-import-confirmation-required"):
            self.commit(preview)
        self.assertEqual(self.commit(preview, True)["result"]["inserted"], 2)

    def test_cancel_and_late_transaction_failure_preserve_old_library(self):
        first = self.commit(self.preview())
        self.api.activate(first["result"]["accountId"])
        library = self.fixture.source._store
        old = [tuple(row) for row in library.connection.execute("SELECT * FROM messages")]
        self.document["messages"][0]["messageId"] = "9007199254741000"
        self.write()
        preview = self.preview()
        original = QQMessageStore.ingest
        def fail(store, account, conv, records, **kwargs):
            def stream():
                for item in records:
                    yield item
                    raise ImportCancelled()
            return original(store, account, conv, stream(), **kwargs)
        with patch.object(QQMessageStore, "ingest", fail):
            result = self.commit(preview)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual([tuple(row) for row in library.connection.execute("SELECT * FROM messages")], old)

    def test_cancel_preview_and_new_preview_reject_old_handle(self):
        preview = self.preview()
        database = self.imports.job["database"]
        self.assertEqual(self.imports.cancel(preview["jobId"])["state"], "cancelled")
        self.assertFalse(database.exists())
        new = self.preview()
        with self.assertRaisesRegex(ValueError, "unknown-import-job"):
            self.imports.commit(preview["jobId"], preview["previewToken"])
        self.assertEqual(new["state"], "ready")

    def test_partial_single_json_and_trailing_data_fail_without_writes(self):
        for content in ('{"messages":[', self.path.read_text(encoding="utf-8") + "{}"):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(self.preview()["state"], "failed")
            self.assertFalse(self.fixture.data_root.exists())

    def test_metadata_after_large_messages_array_and_budget_checks(self):
        self.document = {"messages": self.document["messages"] * 1500, "chatInfo": self.document["chatInfo"],
                         "metadata": self.document["metadata"]}
        self.write()
        self.assertEqual(self.preview()["preview"]["duplicatesInFile"], 2998)
        self.imports.max_rows = 2999
        self.assertEqual(self.preview()["error"], "row-budget-exceeded")
        self.imports.max_rows = 4000
        self.imports.max_bytes = 100
        self.assertEqual(self.preview()["error"], "byte-budget-exceeded")

    def test_group_requires_a_group_code_and_private_scope_still_rejects_multiple_peers(self):
        self.document["chatInfo"]["type"] = "group"
        self.write()
        group=self.preview()
        self.assertEqual((group['state'],group['reason']),('identity','group-code-required'))
        self.assertFalse(self.fixture.data_root.exists())
        self.document["chatInfo"]["type"] = "private"
        third = copy.deepcopy(self.document["messages"][0])
        third["sender"]["uin"] = "10003"
        self.document["messages"].append(third)
        self.write()
        self.assertEqual(self.preview()["error"], "not-single-chat")

    def chunks(self):
        rows = self.document.pop("messages")
        self.document["chunked"] = {"format": "jsonl", "chunksDir": "chunks",
                                   "chunks": [{"fileName": "0001.jsonl"}]}
        directory = self.path.parent / "chunks"
        directory.mkdir()
        path = directory / "0001.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        self.write()
        return path

    def test_chunked_jsonl_matches_single_and_bad_lines_are_counted(self):
        chunk = self.chunks()
        preview = self.preview()
        self.assertEqual((preview["state"], preview["format"]), ("ready", "qce-chunked-jsonl"))
        chunk.write_text(chunk.read_text(encoding="utf-8") + "{\n", encoding="utf-8")
        preview = self.preview()
        self.assertEqual(preview["preview"]["rejected"][0]["reason"], "invalid-json-line")
        self.assertEqual(self.commit(preview, True)["result"]["inserted"], 2)

    def test_missing_chunk_outside_paths_and_declared_count_fail(self):
        chunk = self.chunks()
        self.document["chunked"]["chunks"][0]["messageCount"] = 10
        self.write()
        self.assertEqual(self.preview()["error"], "chunk-count-mismatch")
        self.document["chunked"]["chunks"][0] = {"relativePath": "../outside.jsonl"}
        self.write()
        self.assertEqual(self.preview()["error"], "invalid-chunk-path")
        self.document["chunked"]["chunks"][0] = {"fileName": "missing.jsonl"}
        self.write()
        self.assertEqual(self.preview()["error"], "missing-export-file")

    def test_preview_snapshot_stays_stable_when_original_changes_after_read(self):
        preview = self.preview()
        self.path.write_text("bad", encoding="utf-8")
        self.assertEqual(self.commit(preview)["result"]["inserted"], 2)

    def test_account_deletion_invalidates_pending_preview(self):
        first = self.commit(self.preview())
        preview = self.preview()
        self.api.delete(first["result"]["accountId"])
        with self.assertRaisesRegex(ValueError, "stale-import-preview"):
            self.commit(preview)
        self.assertEqual(self.api.list()["accounts"], [])

    def test_http_import_routes_are_host_protected_and_strict(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.fixture.backend, self.api))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        request = Request(base + "/api/qq/import/preview", data=json.dumps({"path": str(self.path)}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            preview = self.wait(json.load(response))
        with urlopen(base + "/api/qq/import?jobId=" + preview["jobId"], timeout=3) as response:
            self.assertEqual(json.load(response)["state"], "ready")
        request.add_header("Origin", "https://untrusted.example")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_cancel_during_streaming_discards_the_staging_library(self):
        import qq_import
        original = qq_import.stream_export
        entered = threading.Event()
        def blocked(path, on_row, **options):
            def row(value, reason):
                on_row(value, reason)
                entered.set()
                self.imports.job["cancel"].wait(5)
                options["cancel"]()
            return original(path, row, **options)
        with patch.object(qq_import, "stream_export", blocked):
            preview = self.imports.start(str(self.path))
            self.assertTrue(entered.wait(3))
            database = self.imports.job["database"]
            result = self.imports.cancel(preview["jobId"])
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(database.exists())
        self.assertFalse(self.fixture.data_root.exists())

    def test_file_mutation_during_read_is_a_fatal_error(self):
        import qq_import
        original = qq_import.stream_export
        def changed(path, on_row, **options):
            modified = False
            def row(value, reason):
                nonlocal modified
                on_row(value, reason)
                if not modified:
                    modified = True
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(" ")
            return original(path, row, **options)
        with patch.object(qq_import, "stream_export", changed):
            result = self.preview()
        self.assertEqual(result["error"], "export-changed-during-read")
        self.assertFalse(self.fixture.data_root.exists())

    def test_missing_peer_uid_can_be_explicitly_mapped_without_guessing(self):
        del self.document["chatInfo"]["peerUid"]
        for row in self.document["messages"]:
            del row["sender"]["uid"]
        self.write()
        preview = self.preview()
        self.assertEqual(preview["reason"], "peer-uid-required")
        with self.assertRaisesRegex(ValueError, "invalid-peer-uid"):
            self.imports.map_owner(preview["jobId"], "10001", "10002")
        mapped = self.wait(self.imports.map_owner(preview["jobId"], "10001", "u_synthetic_peer"))
        self.assertEqual(mapped["state"], "ready")
        self.assertEqual(self.commit(mapped)["result"]["inserted"], 2)

    def test_declared_empty_and_wrong_encoding_fail_without_creating_data(self):
        self.document["messages"] = []
        self.write()
        preview = self.preview()
        with self.assertRaisesRegex(ValueError, "no-importable-messages"):
            self.commit(preview)
        self.path.write_bytes(b'\xff\xfeinvalid')
        self.assertEqual(self.preview()["error"], "invalid-export-encoding")
        self.assertFalse(self.fixture.data_root.exists())

    def test_existing_account_nickname_survives_import(self):
        first = self.commit(self.preview())
        self.api.activate(first["result"]["accountId"])
        account, directory = self.fixture.source.identity()
        self.api.store.register(account, directory, owner_uin="10001", nickname="用户原有昵称")
        self.commit(self.preview())
        self.assertEqual(self.api.list()["accounts"][0]["nickname"], "用户原有昵称")

    def test_same_body_with_distinct_native_ids_is_not_deduplicated(self):
        extra = copy.deepcopy(self.document["messages"][0])
        extra["messageId"] = "9007199254741050"
        self.document["messages"].append(extra)
        self.write()
        preview = self.preview()
        self.assertEqual(preview["preview"]["uniqueMessages"], 3)
        self.assertEqual(self.commit(preview)["result"]["inserted"], 3)

    def test_reused_peer_uid_with_a_different_known_uin_does_not_merge(self):
        first = self.commit(self.preview())
        self.api.activate(first["result"]["accountId"])
        self.document["chatInfo"]["peerUin"] = "10003"
        self.document["messages"][0]["sender"]["uin"] = "10003"
        self.document["messages"][0]["messageId"] = "9007199254741200"
        self.write()
        result = self.commit(self.preview())
        self.assertEqual((result["state"], result["error"]), ("failed", "peer-identity-mismatch"))
        self.assertEqual(self.fixture.source._store.counts(first["result"]["account"], first["result"]["conversationKey"])[0], 2)

    def test_import_does_not_overwrite_an_existing_sync_checkpoint(self):
        from test_qq_message_store import CHECKPOINT
        first = self.commit(self.preview())
        self.api.activate(first["result"]["accountId"])
        account, conv = first["result"]["account"], first["result"]["conversationKey"]
        library = self.fixture.source._store
        library.ingest(account, conv, [], checkpoint=CHECKPOINT)
        before = [tuple(row) for row in library.connection.execute("SELECT * FROM sync_checkpoints")]
        self.commit(self.preview())
        self.assertEqual([tuple(row) for row in library.connection.execute("SELECT * FROM sync_checkpoints")], before)

    def test_shutdown_cancels_pending_preview_and_rejects_new_work(self):
        preview = self.preview()
        database = self.imports.job["database"]
        self.imports.close()
        self.assertEqual(self.imports.status(preview["jobId"])["state"], "cancelled")
        self.assertFalse(database.exists())
        with self.assertRaisesRegex(ValueError, "import-closing"):
            self.imports.start(str(self.path))

    def test_reads_and_account_switch_do_not_wait_for_a_long_import_transaction(self):
        import qq_message_store
        first = self.commit(self.preview())
        second, _ = self.fixture.bind("10003")
        self.api.activate(first["result"]["accountId"])
        self.document["messages"][0]["messageId"] = "9007199254741200"
        self.write()
        preview = self.preview()
        entered, release = threading.Event(), threading.Event()
        original = qq_message_store._write_record
        def blocked(*values, **options):
            result = original(*values, **options)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("synthetic blocked write timed out")
            return result
        with patch.object(qq_message_store, "_write_record", blocked):
            self.imports.commit(preview["jobId"], preview["previewToken"])
            self.assertTrue(entered.wait(3))
            observed, finished = [], threading.Event()
            def read_and_switch():
                try:
                    observed.extend(self.fixture.source.messages(first["result"]["conversationKey"], 80))
                    self.api.activate(account_id(second))
                finally:
                    finished.set()
            reader = threading.Thread(target=read_and_switch, daemon=True)
            reader.start()
            try:
                self.assertTrue(finished.wait(1), "source lock was held by the import writer")
                self.assertEqual(len(observed), 2, "read must see the complete old transaction")
                self.assertEqual(self.fixture.source.identity()[0], second)
            finally:
                release.set()
                reader.join(timeout=5)
            result = self.wait(preview)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["result"]["account"], first["result"]["account"])
        self.assertEqual(self.fixture.source.identity()[0], second)

    def test_deletion_drains_and_rolls_back_a_running_import_before_file_removal(self):
        import qq_message_store
        first = self.commit(self.preview())
        self.fixture.bind("10003")
        self.document["messages"][0]["messageId"] = "9007199254741200"
        self.write()
        preview = self.preview()
        entered = threading.Event()
        original = qq_message_store._write_record
        def blocked(*values, **options):
            result = original(*values, **options)
            entered.set()
            self.imports.job["cancel"].wait(5)
            return result
        with patch.object(qq_message_store, "_write_record", blocked):
            self.imports.commit(preview["jobId"], preview["previewToken"])
            self.assertTrue(entered.wait(3))
            self.api.delete(first["result"]["accountId"])
        self.assertEqual(self.imports.status(preview["jobId"])["state"], "cancelled")
        self.assertEqual([account["displayId"] for account in self.api.list()["accounts"]], ["10003"])


class ImportAnalysisPipeline(unittest.TestCase):
    def test_actual_import_backfill_rebuilds_to_the_clean_batch_result(self):
        import test_qq_analysis_revision as revisions
        from qq_account_api import QQAccountAPI
        fixture = revisions.RevisionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.seed()
        accounts = QQAccountAPI(fixture.backend, fixture.root / "QQVibeData")
        self.addCleanup(accounts.imports.close)
        document = copy.deepcopy(GOLDEN["export"])
        document["chatInfo"]["peerUid"] = revisions.CONV[2:]
        document["messages"] = document["messages"][:1]
        document["messages"][0]["sender"]["uid"] = revisions.CONV[2:]
        document["messages"][0]["timestamp"] = "1970-01-01T00:00:01.000Z"
        path = fixture.root / "backfill.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        preview = accounts.imports.start(str(path))
        accounts.imports.job["thread"].join(timeout=5)
        preview = accounts.imports.status(preview["jobId"])
        self.assertEqual(preview["state"], "ready")
        accounts.imports.commit(preview["jobId"], preview["previewToken"])
        accounts.imports.job["thread"].join(timeout=5)
        result = accounts.imports.status(preview["jobId"])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["result"]["backfilled"], 1)
        fixture.run_job()
        self.assertEqual(fixture.snapshot(), fixture.golden())


if __name__ == "__main__":
    unittest.main()

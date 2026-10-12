"""Synthetic reset, late-writer and metadata-only batch removal checks."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend_contracts import AccountChangedError, LOCAL_SOURCE_ID, empty_api_portrait
from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from result_store import ResultStore
from real_http import make_handler


class Source:
    def __init__(self, root):
        self.account = "wxid_synthetic_a"
        self.workdir = root / "snapshot"

    def verified_identity(self, **_kwargs):
        return self.account, self.workdir


class Analyzer:
    model = {"state": "ready"}

    def __init__(self):
        self.clears = 0

    def analysis_version(self):
        return "synthetic-version"

    def clear_caches(self):
        self.clears += 1


class AnalysisScopeResetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = Source(self.root)
        self.analyzer = Analyzer()
        self.store = ResultStore(self.root / "results.sqlite3")
        self.backend = Backend(self.source, self.analyzer,
                               store_factory=lambda *_args: self.store,
                               selection_store=ConversationSelectionStore(self.root / "selection"))
        self.addCleanup(self.backend.shutdown)

    def seed_fine(self, user="friend"):
        with self.store.connect() as conn:
            conn.execute("INSERT INTO fine_skips_v1 VALUES (?,?,?,?,?,?,?,?)",
                         (self.source.account, user, "synthetic-id", "synthetic-version", 1,
                          "message__message_0.db", 1, "synthetic-skip"))

    def known(self, user="friend"):
        return self.store.fine_known(self.source.account, user, "synthetic-version", ["synthetic-id"])

    def test_conversation_reset_drops_queued_job_only_in_scope(self):
        self.seed_fine()
        self.seed_fine("other-friend")
        key = (self.source.account, str(self.store.path), "friend", "synthetic-version")
        old_job = {"status": "queued"}
        other_job = {"status": "queued"}
        self.backend.jobs[key] = old_job
        self.backend.jobs[(key[0], key[1], "other-friend", key[3])] = other_job
        result = self.backend.analysis_scope_clear(self.source.account, "friend", LOCAL_SOURCE_ID, "conversation")
        self.assertTrue(result["cleared"])
        self.assertFalse(self.known())
        self.assertTrue(self.known("other-friend"))
        self.assertTrue(old_job["_cancelledByReset"])
        self.assertNotIn("_cancelledByReset", other_job)
        self.assertEqual(self.analyzer.clears, 1)

    def test_portrait_reset_keeps_message_labels_and_marks_rebuild(self):
        self.seed_fine()
        self.backend.analysis_scope_clear(self.source.account, "friend", LOCAL_SOURCE_ID, "portrait")
        self.assertTrue(self.known())
        self.assertTrue(self.store.portrait_reset(self.source.account, "friend", "friend"))

    def test_reset_busy_or_wrong_account_never_deletes(self):
        self.seed_fine()
        with self.assertRaises(AccountChangedError):
            self.backend.analysis_scope_clear("other-account", "friend", LOCAL_SOURCE_ID, "conversation")
        self.backend._key_lock((self.source.account, str(self.store.path), "friend", "synthetic-version"))
        with patch.object(self.backend, "_key_lock", return_value=SimpleNamespace(acquire=lambda **_kwargs: False)):
            with self.assertRaisesRegex(RuntimeError, "analysis-reset-busy"):
                self.backend.analysis_scope_clear(self.source.account, "friend", LOCAL_SOURCE_ID, "conversation")
        self.assertTrue(self.known())
        self.assertFalse(self.backend.analysis_scope_resets)

    def test_api_reset_invalidates_late_writer_without_touching_other_scope(self):
        api = "a" * 32
        self.store.register_api_source(self.source.account, api, "chat_completions", "synthetic-model")
        self.backend.active_model_source_mode = "api"
        self.backend.active_model_source_id = api
        self.backend.active_api_config = {"contextTokens": 8192, "model": "synthetic-model",
                                          "protocol": "chat_completions"}
        key = (self.source.account, "friend", api, "friend")
        old = {"status": "running"}
        other = {"status": "running"}
        self.backend.api_portrait_jobs[key] = old
        self.backend.api_portrait_jobs[(self.source.account, "other-friend", api, "other-friend")] = other
        self.backend.analysis_scope_clear(self.source.account, "friend", api, "portrait")
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(other["status"], "running")
        with self.assertRaisesRegex(RuntimeError, "analysis-cancelled"):
            self.backend._assert_api_portrait_job(key, old, self.store, self.backend.active_api_config)

    def test_account_switch_clears_tokenizer_cache_without_model_start(self):
        self.backend._scoped_identity()
        self.source.account = "wxid_synthetic_b"
        self.backend._scoped_identity()
        self.assertEqual(self.analyzer.clears, 1)

    def test_profile_read_and_reset_serialize_before_the_clear_returns(self):
        started, finish, reset_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def old_profile(*_args):
            started.set()
            if not finish.wait(3):
                raise RuntimeError("synthetic profile not released")
            self.store.clear_scope(self.source.account, "friend", LOCAL_SOURCE_ID, subject="friend")
        def read():
            try:
                self.backend.profile("friend")
            except Exception as exc:
                errors.append(exc)
        def reset():
            try:
                self.backend.analysis_scope_clear(self.source.account, "friend", LOCAL_SOURCE_ID, "conversation")
                reset_done.set()
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.backend, "_profile_locked", side_effect=old_profile):
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(started.wait(3))
            clear = threading.Thread(target=reset)
            clear.start()
            self.assertFalse(reset_done.wait(.05))
            finish.set()
            reader.join(3)
            clear.join(3)
        self.assertFalse(reader.is_alive())
        self.assertFalse(clear.is_alive())
        self.assertFalse(errors)
        self.assertTrue(reset_done.is_set())
        self.assertFalse(self.store.portrait_reset(self.source.account, "friend", "friend"))

    def test_saved_profile_read_does_not_wait_for_inflight_model_worker(self):
        key = (self.source.account, str(self.store.path), "friend", "synthetic-version")
        lock = self.backend._key_lock(key)
        lock.acquire()
        try:
            with patch.object(self.backend, "_profile_locked", return_value={"synthetic": True}):
                self.assertEqual(self.backend.profile("friend"), {"synthetic": True})
        finally:
            lock.release()

    def test_batch_remove_is_atomic_and_does_not_read_messages(self):
        for user in ("friend", "other-friend", "third-friend"):
            self.backend.selection_store.set_selected(self.source.account, user, True)
        self.seed_fine()
        with self.assertRaises(ValueError):
            self.backend.remove_conversations_selected(self.source.account, ["friend", "missing"])
        self.assertEqual(len(self.backend.selection_store.get(self.source.account)["selectedSessions"]), 3)
        state = self.backend.remove_conversations_selected(self.source.account, ["friend", "third-friend"])
        self.assertEqual(state["selectedSessions"], ["other-friend"])
        self.assertTrue(self.known())

    def test_http_reset_validates_shape_and_preserves_confirmation_scope(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def post(body):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("POST", "/api/analysis-scope/clear", json.dumps(body),
                                       {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            body = {"account": self.source.account, "user": "friend", "sourceId": LOCAL_SOURCE_ID,
                    "kind": "portrait"}
            self.assertEqual(post({**body, "unknown": True})[0], 400)
            self.assertEqual(post({**body, "member": "other"})[0], 400)
            status, response = post(body)
            self.assertEqual(status, 200)
            self.assertEqual({key: response[key] for key in body}, body)
            self.assertTrue(response["cleared"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_preparation_diagnostics_expose_only_public_status(self):
        self.source.factory = SimpleNamespace(preparation_status=lambda _account: {
            "reason": "scan_limit", "state": "failed", "message": "Synthetic scan reached its budget",
            "retryAfterSeconds": 30, "keys": "synthetic-secret", "path": "synthetic-private"})
        status = self.backend.preparation_status()
        self.assertEqual(set(status), {"reason", "state", "message", "retryAfterSeconds"})
        self.assertNotIn("synthetic-secret", json.dumps(status))

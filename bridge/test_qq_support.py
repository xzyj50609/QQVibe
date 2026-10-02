"""Fixed-field redaction and scoped summaries against real temporary QQ libraries."""
import http.client
import json
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from backend_contracts import AccountChangedError
from product_profile import load_product
from qq_message_store import QQMessageStore
from qq_source import QQSource
from qq_support import diagnostics
from qq_sync_work import initial, advance, write
from test_qq_message_store import Library, CONV, UIN, CHECKPOINT, record
import test_qq_accounts as accounts_fixture
from real_http import make_handler


class DiagnosticTests(unittest.TestCase):
    def test_only_fixed_enums_counts_and_software_metadata_leave_the_process(self):
        secret = "SYNTHETIC_PRIVATE_BODY_OWNER_PATH_AND_KEY"
        backend = SimpleNamespace(
            health=lambda: {"ok": True, "data": {"state": "ready", "account": secret, "error": secret},
                            "model": {"state": "ready", "provider": "cpu", "message": secret}},
            model_source=lambda: {"mode": "api", "sourceId": secret,
                "api": {"protocol": "responses", "baseUrl": secret, "model": secret, "apiKey": secret}},
            messages=Mock(side_effect=AssertionError("must not read bodies")),
            runtime=Mock(side_effect=AssertionError("must not start a model worker")))
        sync = {"state": "partial", "reason": "page-repeated", "account": secret,
                "ownerUin": secret, "baseUrl": secret, "token": secret,
                "requestsTotal": 8, "lastSuccessAtMs": 1700000000000,
                "conversations": {secret: {"name": secret, "state": "partial",
                    "history": {"state": "error", "reason": secret, "raw": secret}}}}
        accounts = SimpleNamespace(sync_manager=SimpleNamespace(public=lambda: sync), imports=None)
        result = diagnostics(backend, accounts, "1.2.2")
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertEqual(result["sync"]["requestsTotal"], 8)
        self.assertEqual(result["sync"]["states"]["history"], {"error": 1})
        self.assertEqual(result["service"]["protocol"], "responses")
        self.assertEqual(result["software"]["appVersion"], "1.2.2")
        backend.messages.assert_not_called(); backend.runtime.assert_not_called()

    def test_unknown_strings_exception_details_and_invalid_counts_cannot_be_exported(self):
        secret = "SYNTHETIC_ERROR_CONTAINING_A_PATH_AND_ACCOUNT"
        backend = SimpleNamespace(health=Mock(side_effect=RuntimeError(secret)), model_source=lambda: [])
        accounts = SimpleNamespace(sync_manager=SimpleNamespace(public=lambda: {
            "state": secret, "reason": secret, "requestsTotal": True, "lastSuccessAtMs": 10**20}), imports=None)
        result = diagnostics(backend, accounts, secret)
        self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(result["available"]["health"])
        self.assertFalse(result["available"]["modelSelection"])
        self.assertEqual(result["sync"]["reason"], "unknown")
        self.assertIsNone(result["sync"]["requestsTotal"])
        self.assertIsNone(result["sync"]["lastSuccessAtMs"])

    def test_summary_sampling_is_bounded_without_disclosing_conversation_keys(self):
        backend = SimpleNamespace(health=lambda: {}, model_source=lambda: {})
        accounts = SimpleNamespace(sync_manager=SimpleNamespace(public=lambda: {
            "conversations": {f"PRIVATE_SYNTHETIC_{i}": {"state": "complete"} for i in range(600)}}), imports=None)
        result = diagnostics(backend, accounts, "1.2.2")
        self.assertEqual(result["sync"]["conversationCount"], 600)
        self.assertEqual(result["sync"]["sampledConversations"], 500)
        self.assertEqual(result["sync"]["states"]["forward"], {"complete": 500})
        self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(result))


class ScopeTests(Library):
    def setUp(self):
        super().setUp()
        self.source = QQSource(uin=UIN, store=self.db, root=self.root, profile=load_product("qq"))
        self.addCleanup(self.source.close)

    def scope(self):
        return self.source.data_scope(self.account, CONV)

    def test_empty_local_scope_is_unknown_coverage_and_creates_no_auxiliary_tables(self):
        before = self.rows("SELECT name,sql FROM sqlite_master ORDER BY name")
        result = self.scope()
        self.assertEqual(result["counts"], {"messages": 0, "validTexts": 0, "peerValidTexts": 0})
        self.assertIsNone(result["range"]["startMs"])
        self.assertIsNone(result["forward"])
        self.assertEqual(result["completeness"], "not-proven")
        self.assertEqual(before, self.rows("SELECT name,sql FROM sqlite_master ORDER BY name"))

    def test_counts_and_versions_keep_quotes_placeholders_and_recall_out_of_valid_text(self):
        self.db.ingest(self.account, CONV, [record("1", text="SYNTHETIC_BODY", quote="SYNTHETIC_QUOTE"),
            record("2", direction="self"), record("3", kind="image", text=None),
            record("4", status="recalled", recall_time="1"), record("5", text=" \n "),
            record("6", direction="system")])
        result = self.scope()
        self.assertEqual(result["counts"], {"messages": 6, "validTexts": 2, "peerValidTexts": 1})
        self.assertEqual(result["normalizeVersions"], [{"version": "qq-v1", "messages": 6}])
        self.assertNotIn("SYNTHETIC_BODY", json.dumps(result))
        self.assertNotIn("SYNTHETIC_QUOTE", json.dumps(result))

    def test_backfill_changes_range_and_revision_without_claiming_full_history(self):
        self.db.ingest(self.account, CONV, [record("late", time_ms=10000)])
        old = self.scope()
        self.db.ingest(self.account, CONV, [record("early", time_ms=1000)])
        result = self.scope()
        self.assertEqual(result["range"], {"startMs": 1000, "endMs": 10000})
        self.assertGreater(result["dataRevision"], old["dataRevision"])
        self.assertEqual(result["completeness"], "not-proven")

    def test_metadata_and_counts_share_one_snapshot_when_another_connection_commits(self):
        self.db.ingest(self.account, CONV, [record("late", time_ms=10000)])
        before = self.scope()
        connection = self.db.connection
        # WAL permits a real other-connection commit while the reader is active.
        # Only this temporary fixture changes journal mode; production stays unchanged.
        connection.execute("PRAGMA journal_mode=WAL")
        account = self.account
        with closing(QQMessageStore(self.db.path)) as writer:
            class Interleaved:
                committed = False
                def __getattr__(self, name): return getattr(connection, name)
                def execute(self, sql, values=()):
                    cursor = connection.execute(sql, values)
                    if sql.startswith("SELECT data_revision") and not self.committed:
                        self.committed = True
                        writer.ingest(account, CONV, [record("early", time_ms=1000)])
                    return cursor
            self.db.connection = Interleaved()
            try: interleaved = self.scope()
            finally: self.db.connection = connection
        self.assertEqual(interleaved, before, "revision and range/counts must describe the same committed snapshot")
        after = self.scope()
        self.assertEqual(after["counts"]["messages"], 2)
        self.assertGreater(after["dataRevision"], before["dataRevision"])
        self.assertEqual(after["range"], {"startMs": 1000, "endMs": 10000})

    def test_forward_window_and_persistent_pending_history_survive_a_new_store_connection(self):
        self.db.ingest(self.account, CONV, [record("1")], checkpoint=CHECKPOINT)
        payload = initial("history", 100000)
        for _ in range(3): payload = advance("history", payload, "partial", "budget-exhausted", 200000)
        self.db.ensure_sync_work()
        with self.db.transaction() as cursor: write(cursor, self.account, CONV, "history", 0, payload)
        before = self.scope()
        self.assertEqual(before["history"]["stack"], [[0, 50000]])
        self.assertEqual(before["history"]["window"], [50000, 100000])
        self.assertEqual(before["forward"]["state"], "complete")
        self.assertEqual(before["scanBoundary"], "interface-window-only")
        with closing(QQMessageStore(self.db.path)) as reopened:
            other = QQSource(uin=UIN, store=reopened, root=self.root, profile=load_product("qq"))
            self.assertEqual(before, other.data_scope(self.account, CONV))

    def test_cross_account_and_unknown_conversation_are_rejected(self):
        with self.assertRaises(AccountChangedError): self.source.data_scope("a:" + "0" * 32, CONV)
        with self.assertRaisesRegex(ValueError, "unknown-local-conversation"):
            self.source.data_scope(self.account, "u:u_missing")


class SupportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = accounts_fixture.QQAccountTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.account, _ = self.fixture.bind(UIN)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.fixture.backend, self.fixture.api))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.thread.join, 3); self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)) as client:
            client.request("GET", path, headers=headers or {})
            response = client.getresponse()
            return response.status, json.loads(response.read())

    def test_formal_routes_return_redacted_diagnostics_and_current_scope(self):
        status, report = self.get("/api/qq/diagnostics")
        self.assertEqual(status, 200); self.assertEqual(report["schema"], "qq-diagnostics-v1")
        self.assertNotIn(self.account, json.dumps(report)); self.assertNotIn(f'"{UIN}"', json.dumps(report))
        status, scope = self.get("/api/qq/data-scope?" + urlencode({"account": self.account, "user": CONV}))
        self.assertEqual(status, 200); self.assertEqual(scope["counts"]["messages"], 1)

    def test_untrusted_origin_cannot_read_either_route_or_trigger_collection(self):
        with patch.object(self.fixture.backend, "health", side_effect=AssertionError("must not collect")):
            for route in ("/api/qq/diagnostics", "/api/qq/data-scope"):
                status, report = self.get(route, {"Origin": "https://untrusted.example"})
                self.assertEqual(status, 403); self.assertEqual(report, {"error": "forbidden"})

    def test_wrong_account_is_a_scoped_conflict(self):
        status, _ = self.get("/api/qq/data-scope?" + urlencode({"account": "a:" + "0" * 32, "user": CONV}))
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()

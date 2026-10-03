"""Atomic source receipts against isolated SQLite, exports and loopback QCE."""
import json
import sqlite3
import unittest
from contextlib import contextmanager, closing
from unittest.mock import patch
from urllib.parse import urlencode

import qq_ingest_audit as audit
import qq_message_store as store
from qq_source import QQSource
from qq_sync import QQSync
from product_profile import load_product
from backend_contracts import AccountChangedError
from test_qq_message_store import Library, record, CONV, UIN
import test_qq_import as import_fixture
import test_qq_sync as sync_fixture
import test_qq_support as http_fixture


def receipt(kind="forward", state="complete", reason=None, rejected=0):
    value = {"kind": kind, "format": "qce-api", "status": state, "reason": reason,
             "windowStartMs": 0, "windowEndMs": 1700001000000, "sourceVersion": "6.3.0", "rejectedRows": rejected}
    if kind == "file-import":
        value.update(format="qce-single-json", sourceSnapshot="c" * 64, sourceVersion=None,
                     windowStartMs=None, windowEndMs=None)
    return value


class AtomicReceiptTests(Library):
    def summary(self): return audit.summary(self.db.connection, self.account, CONV)
    def page(self, **options): return audit.page(self.db.connection, self.account, CONV, **options)

    def test_reading_legacy_library_never_creates_tables_or_invents_origins(self):
        self.db.ingest(self.account, CONV, [record("1")])
        before = self.rows("SELECT name,sql FROM sqlite_master ORDER BY name")
        self.assertEqual(self.page()["entries"], [])
        self.assertEqual(self.summary()["recordedMessages"], 0)
        source = QQSource(uin=UIN, store=self.db, root=self.root, profile=load_product("qq"))
        self.assertEqual(source.data_scope(self.account, CONV)["provenance"]["unrecordedMessages"], 1)
        self.assertEqual(before, self.rows("SELECT name,sql FROM sqlite_master ORDER BY name"))

    def test_auxiliary_migration_preserves_recoverable_old_library(self):
        self.db.ingest(self.account, CONV, [record("old")])
        self.db.ingest(self.account, CONV, [record("new")], receipt=receipt(), now_ms=1000)
        backups = list(self.db.path.parent.glob("*.ingest-audit-backup-*"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertFalse(audit.exists(backup)); self.assertEqual(backup.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.summary()["recordedMessages"], 1)
        self.assertEqual(self.db.connection.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)

    def test_same_message_multiple_actual_sources_does_not_duplicate_or_revise_analysis(self):
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt("file-import"), now_ms=100)
        revision = self.revision()
        for kind in ("forward", "history", "reconcile"):
            result = self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(kind), now_ms=200)
            self.assertEqual(result["unchanged"], 1)
        self.assertEqual(self.revision(), revision); self.assertEqual(len(self.rows("SELECT * FROM messages")), 1)
        summary = self.summary()
        self.assertEqual((summary["recordedMessages"], summary["runCount"], summary["forwardSuccesses"]), (1, 4, 1))
        self.assertEqual({item["kind"] for item in summary["sources"]}, audit.KINDS)

    def test_late_cancel_rolls_back_messages_observations_checkpoint_and_receipt(self):
        self.db.ensure_ingest_audit(); calls = 0
        def cancel():
            nonlocal calls; calls += 1
            if calls == 3: raise RuntimeError("synthetic-cancel")
        with self.assertRaisesRegex(RuntimeError, "synthetic-cancel"):
            self.db.ingest(self.account, CONV, [record("1"), record("2")], receipt=receipt(), cancel=cancel)
        self.assertEqual(self.summary()["runCount"], 0); self.assertFalse(self.rows("SELECT * FROM messages"))
        self.assertFalse(self.rows("SELECT * FROM message_observations")); self.assertFalse(self.rows("SELECT * FROM qq_ingest_observations_v1"))

    def test_commit_scope_failure_cannot_leave_a_success_receipt(self):
        @contextmanager
        def refused(): raise RuntimeError("synthetic-selection-changed"); yield
        with self.assertRaisesRegex(RuntimeError, "selection-changed"):
            self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(), commit_scope=refused)
        self.assertEqual(self.summary()["forwardSuccesses"], 0); self.assertFalse(self.rows("SELECT * FROM messages"))

    def test_receipt_clock_failure_also_rolls_back_the_ingest(self):
        def clock(): raise RuntimeError("synthetic-clock-failure")
        with self.assertRaisesRegex(RuntimeError, "clock-failure"):
            self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(), receipt_clock=clock)
        self.assertEqual(self.summary()["runCount"], 0); self.assertFalse(self.rows("SELECT * FROM messages"))
        self.assertFalse(self.rows("SELECT * FROM qq_ingest_message_sources_v1"))

    def test_corrupt_source_projection_is_rejected_before_backup(self):
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(), now_ms=1)
        with self.db.transaction() as cursor:
            cursor.execute("UPDATE qq_ingest_message_sources_v1 SET source_mask=8")
        with self.assertRaisesRegex(store.StoreError, "audit is invalid"):
            self.db.backup_to(self.root / "wrong-projection.sqlite")
        self.assertFalse((self.root / "wrong-projection.sqlite").exists())

    def test_partial_and_error_never_claim_forward_success_but_empty_success_survives(self):
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(state="partial", reason="page-repeated"), now_ms=1)
        self.db.ingest(self.account, CONV, [], receipt=receipt(state="error", reason="auth-required"), now_ms=2)
        self.assertEqual(self.summary()["forwardSuccesses"], 0)
        self.db.ingest(self.account, CONV, [], receipt=receipt(state="complete-empty"), now_ms=3)
        self.assertEqual((self.summary()["forwardSuccesses"], self.summary()["lastForwardSuccessAtMs"]), (1, 3))
        for state in ("error", "complete-empty"):
            with self.assertRaisesRegex(ValueError, "empty-state"):
                self.db.ingest(self.account, CONV, [record("bad")], receipt=receipt(state=state), now_ms=4)
        self.assertEqual(len(self.rows("SELECT * FROM messages")), 1)

    def test_final_clock_is_taken_after_accepting_rows(self):
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(), now_ms=100, receipt_clock=lambda: 150)
        self.assertEqual(self.summary()["lastForwardSuccessAtMs"], 150)

    def test_wall_clock_rollback_does_not_change_which_success_is_latest(self):
        self.db.ingest(self.account, CONV, [], receipt=receipt(state="complete-empty"), now_ms=1000)
        self.db.ingest(self.account, CONV, [], receipt=receipt(state="complete-empty"), now_ms=500)
        self.assertEqual(self.summary()["lastForwardSuccessAtMs"], 500)
        self.assertEqual(audit.last_success(self.db.connection, self.account), 500)

    def test_invalid_receipt_does_not_migrate_or_store_credentials(self):
        for change in ({"token": "SYNTHETIC_SECRET"}, {"reason": "C:/PRIVATE/PATH"}, {"kind": []},
                       {"windowStartMs": True}, {"sourceVersion": "<img>"}, {"rejectedRows": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.db.ingest(self.account, CONV, [record("1")], receipt={**receipt(), **change})
        self.assertFalse(audit.exists(self.db.connection)); self.assertFalse(self.rows("SELECT * FROM messages"))

    def test_run_pagination_is_complete_stable_and_filterable(self):
        for number in range(27):
            self.db.ingest(self.account, CONV, [record("1")], receipt=receipt("history" if number % 2 else "forward"), now_ms=number)
        seen, before = [], None
        while True:
            result = self.page(before=before, limit=4)
            seen.extend(item["runId"] for item in result["entries"]); before = result["nextBefore"]
            if before is None: break
        self.assertEqual(seen, list(range(27, 0, -1)))
        self.assertEqual(len(self.page(kind="forward", limit=50)["entries"]), 14)

    def test_competing_version_samples_are_bounded_without_losing_later_runs(self):
        self.db.ingest(self.account, CONV, [record("1", text=str(number)) for number in range(8)], receipt=receipt())
        self.db.ingest(self.account, CONV, [record("1", text="0")], receipt=receipt("history"))
        key = self.rows("SELECT message_key FROM messages")[0][0]
        latest = self.page(key=key, limit=1); older = self.page(key=key, limit=1, before=latest["nextBefore"])
        self.assertEqual(latest["entries"][0]["runId"], 2); self.assertEqual(older["entries"][0]["runId"], 1)
        self.assertEqual(older["entries"][0]["observationCount"], 8)
        self.assertEqual(len(older["entries"][0]["observations"]), 5)
        self.assertEqual(self.rows("SELECT text FROM messages")[0][0], "0")

    def test_same_run_same_version_observation_dedup_does_not_hide_received_count(self):
        self.db.ingest(self.account, CONV, [record("1"), record("1")], receipt=receipt())
        row = self.page()["entries"][0]
        self.assertEqual((row["acceptedRows"], row["counts"]["inserted"], row["counts"]["unchanged"]), (2, 1, 1))
        self.assertEqual(len(self.rows("SELECT * FROM qq_ingest_observations_v1")), 1)

    def test_backup_restore_keeps_origins_but_does_not_invent_post_backup_success(self):
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt(), now_ms=100)
        backup = self.db.backup_to(self.root / "with-origins.sqlite")
        self.db.ingest(self.account, CONV, [record("2")], receipt=receipt(), now_ms=200)
        self.db.restore_from(backup)
        self.assertEqual((self.summary()["runCount"], self.summary()["lastForwardSuccessAtMs"]), (1, 100))
        self.assertEqual(self.summary()["recordedMessages"], 1)

    def test_cross_conversation_corruption_is_rejected_before_backup(self):
        other = "u:u_other_synthetic"
        self.db.ensure_conversation(self.account, other)
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt())
        other_record = record("2"); other_record["conversation_key"] = other
        self.db.ingest(self.account, other, [other_record])
        key = self.rows("SELECT message_key FROM messages WHERE conversation_key=?", [other])[0][0]
        with self.db.transaction() as cursor:
            cursor.execute("INSERT INTO qq_ingest_observations_v1 VALUES (1,?,?,?,?)", (key, "e" * 64, "qq-v3", "inserted"))
        with self.assertRaisesRegex(store.StoreError, "audit is invalid"):
            self.db.backup_to(self.root / "unsafe.sqlite")
        self.assertFalse((self.root / "unsafe.sqlite").exists())

    def test_read_scoping_and_stale_message_cursor_are_enforced(self):
        self.db.ingest(self.account, CONV, [record("1")], receipt=receipt())
        source = QQSource(uin=UIN, store=self.db, root=self.root, profile=load_product("qq"))
        cursor = source.messages(CONV, 80)[0]["historyCursor"]
        with self.assertRaises(AccountChangedError): source.ingest_history("a:" + "0" * 32, CONV)
        first = source.ingest_history(self.account, CONV, message_cursor=cursor)
        self.assertEqual(len(first["entries"]), 1)
        self.db.ingest(self.account, CONV, [record("1", text="changed")], receipt=receipt("history"))
        with self.assertRaises(ValueError): source.ingest_history(self.account, CONV, message_cursor=cursor)

    def test_invalid_page_inputs_are_rejected_even_for_legacy_libraries(self):
        for options in ({"before": 0}, {"before": True}, {"limit": 0}, {"limit": 51}, {"kind": "unknown"}):
            with self.subTest(options=options), self.assertRaises(ValueError): self.page(**options)

    def test_partial_auxiliary_schema_is_not_silently_repaired(self):
        self.db.ensure_ingest_audit()
        self.db.connection.execute("DROP INDEX idx_qq_ingest_runs_scope")
        with self.assertRaisesRegex(ValueError, "audit-schema"):
            self.db.ensure_ingest_audit()


class FormalIngestTests(unittest.TestCase):
    def importer(self):
        fixture = import_fixture.ImportTests(); self.addCleanup(fixture.doCleanups); fixture.setUp()
        return fixture

    def test_formal_import_persists_parsed_snapshot_and_origins_after_reopen(self):
        f = self.importer(); first = f.commit(f.preview()); f.api.activate(first["result"]["accountId"])
        account, user = first["result"]["account"], first["result"]["conversationKey"]
        one = f.fixture.source.ingest_history(account, user)["entries"][0]
        self.assertEqual((one["kind"], one["format"], one["acceptedRows"]), ("file-import", "qce-single-json", 2))
        self.assertEqual(one["sourceVersion"], "6.0.3")
        self.assertRegex(one["sourceSnapshot"], r"^[0-9a-f]{64}$")
        second = f.commit(f.preview()); self.assertEqual(second["result"]["unchanged"], 2)
        two = f.fixture.source.ingest_history(account, user)["entries"][0]
        self.assertEqual(one["sourceSnapshot"], two["sourceSnapshot"])
        path = f.fixture.source._store.path
        f.fixture.source.close()
        with closing(store.QQMessageStore(path)) as reopened:
            source = QQSource(uin=UIN, store=reopened, root=f.fixture.root, profile=load_product("qq"))
            self.assertEqual(source.data_scope(account, user)["provenance"]["recordedMessages"], 2)
            message = source.messages(user, 80)[0]
            self.assertEqual(len(source.ingest_history(account, user, message_cursor=message["historyCursor"])["entries"]), 2)

    def test_formal_sync_success_history_survives_new_coordinator_and_failure(self):
        f = sync_fixture.SyncTests(); self.addCleanup(f.doCleanups); f.setUp()
        self.assertEqual(f.sync.run_once(CONV)["state"], "complete")
        original = f.sync.public()["lastSuccessAtMs"]; self.assertIsNotNone(original)
        f.sync.close()
        restarted = QQSync(f.api, config_store=f.config, live_validated=True, autostart=False)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.public()["lastSuccessAtMs"], original)
        with f.source.read_library(f.account) as (_, library):
            library.ingest(f.account, CONV, [], receipt=receipt(state="error", reason="auth-required"), now_ms=original + 1)
        self.assertEqual(restarted.public()["lastSuccessAtMs"], original)
        self.assertEqual(f.source.data_scope(f.account, CONV)["provenance"]["forwardSuccesses"], 1)

    def test_formal_http_routes_scope_cursor_limits_and_no_diagnostic_identity_leak(self):
        f = http_fixture.SupportHTTPTests(); self.addCleanup(f.doCleanups); f.setUp()
        source = f.fixture.source
        with source.read_library(f.account) as (_, library): library.ingest(f.account, CONV, [record("source-http")], receipt=receipt())
        query = {"account": f.account, "user": CONV}
        status, data = f.get("/api/qq/ingest-history?" + urlencode(query)); self.assertEqual(status, 200); self.assertEqual(len(data["entries"]), 1)
        message = next(m for m in source.messages(CONV, 80) if m["id"] == "source-http")
        route = "/api/qq/message-provenance?" + urlencode({**query, "cursor": message["historyCursor"]})
        self.assertEqual(f.get(route)[0], 200)
        for suffix in ("&limit=51", "&before=0", "&kind=unknown"):
            self.assertEqual(f.get("/api/qq/ingest-history?" + urlencode(query) + suffix)[0], 400)
        self.assertEqual(f.get(route, {"Origin": "https://untrusted.invalid"})[0], 403)
        self.assertEqual(f.get("/api/qq/ingest-history?" + urlencode({**query, "account": "a:" + "0" * 32}))[0], 409)
        status, diagnostic = f.get("/api/qq/diagnostics"); self.assertEqual(status, 200)
        text = json.dumps(diagnostic); self.assertNotIn(f.account, text); self.assertNotIn("source-http", text); self.assertNotIn("fingerprint", text)


if __name__ == "__main__": unittest.main()

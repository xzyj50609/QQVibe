"""Behavior regressions from review 10. Only synthetic, temporary libraries."""
from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from backend_contracts import AccountChangedError
from batch_engine import BatchEngine
from product_profile import load_product
from profile_state import empty_state
from qq_identity import account_key
from qq_message_store import QQMessageStore, StoreError, database_path
from qq_source import QQSource, MEDIA_REASON, decode_cursor
from result_store import ResultStore
from test_qq_message_store import record, CONV, UIN, CHECKPOINT


class Library(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="qq-integrity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = account_key(UIN)
        self.store = QQMessageStore(database_path(self.account, self.root))
        self.store.ensure_conversation(self.account, CONV, peer_uid="u_peer_synth01")
        self.source = QQSource(uin=UIN, store=self.store, root=self.root, profile=load_product("qq"))
        self.addCleanup(self.store.close)
        self.addCleanup(self.source.close)

    def ingest(self, *records, **kwargs):
        return self.store.ingest(self.account, CONV, records, **kwargs)

    def state(self):
        with self.store.lock:
            return ([tuple(row) for row in self.store.connection.execute("SELECT * FROM messages")],
                    [tuple(row) for row in self.store.connection.execute("SELECT * FROM message_conflicts")],
                    self.store.revision(self.account, CONV))


class ReplayTests(Library):
    def test_missing_export_sequence_and_padded_sender_do_not_invent_identity_conflicts(self):
        self.ingest(record("1", native_seq="99", sender_uin="10002"))
        before = self.state()
        outcome = self.ingest(record("1", native_seq=None, sender_uin="0010002"))
        self.assertEqual(outcome["unchanged"], 1)
        self.assertEqual(self.state(), before)
        self.ingest(record("2", native_seq=None))
        before_revision = self.store.revision(self.account, CONV)
        self.assertEqual(self.ingest(record("2", native_seq="77"))["unchanged"], 1)
        self.assertEqual(self.store.revision(self.account, CONV), before_revision)

    def test_competing_body_is_quarantined_and_replays_without_new_revision_or_evidence(self):
        original, changed = record("1", text="原始文本"), record("1", text="修改文本")
        self.ingest(original)
        self.ingest(changed)
        state = self.state()
        for value in (changed, changed, original, original):
            self.assertEqual(self.ingest(value)["unchanged"], 1)
            self.assertEqual(self.state(), state)
        row = self.store.page(self.account, CONV)[0][0]
        self.assertEqual((row["text"], row["raw"], row["status"]),
                         (original["text"], original["raw"], "conflict"))

    def test_recall_is_stable_for_both_arrival_orders_and_unknown_recall_replay(self):
        for index, order in enumerate(("normal-first", "recall-first")):
            with self.subTest(order=order):
                normal = record(str(index))
                recall = record(str(index), status="recalled", recall_time="1700000100")
                self.ingest(*(normal, recall) if order == "normal-first" else (recall, normal))
                state = self.state()
                for value in (normal, recall, dict(normal, recall_time=None), normal):
                    self.ingest(value)
                    self.assertEqual(self.state(), state)
                rows = self.store.page(self.account, CONV)[0]
                self.assertTrue(all(row["status"] == "recalled" for row in rows))

    def test_replay_matrix_preserves_existing_status(self):
        for index, status in enumerate(("normal", "recalled", "conflict", "revised")):
            value = record(str(index), status=status, recall_time="5" if status == "recalled" else "0")
            self.ingest(value)
            before = self.state()
            self.ingest(value, value)
            self.assertEqual(self.state(), before)

    def test_recalled_content_conflict_keeps_recall_and_is_idempotent(self):
        self.ingest(record("1", status="recalled", recall_time="5"))
        changed = record("1", text="另一个版本")
        self.ingest(changed)
        state = self.state()
        self.ingest(changed)
        self.assertEqual(self.state(), state)
        self.assertEqual(self.store.page(self.account, CONV)[0][0]["status"], "recalled")

    def test_evidence_and_observation_fingerprints_roll_back_with_checkpoint(self):
        self.ingest(record("1"))
        before = self.state()
        changed = record("1", text="修改")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest(changed, checkpoint=dict(CHECKPOINT, state="bad"))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ingest(changed)["conflicts"], 1)
        self.assertEqual(self.ingest(changed)["unchanged"], 1)

    def test_moved_timestamp_invalidation_includes_old_and_new_positions(self):
        for index, (old, new) in enumerate(((1000, 9000), (9000, 1000), (1000, 1000))):
            with self.subTest(old=old, new=new):
                key = "u:u_time" + str(index)
                self.store.ensure_conversation(self.account, key)
                records = [dict(record(str(index), time_ms=old), conversation_key=key),
                           dict(record(str(index), time_ms=new, text="修改"), conversation_key=key)]
                self.store.ingest(self.account, key, records[:1])
                self.store.ingest(self.account, key, records[1:])
                self.assertEqual(self.store.revision(self.account, key)[1:], (min(old, new), 1))


class ScopeAndThreadTests(Library):
    def test_read_and_write_on_different_threads_use_the_same_serialized_library(self):
        results, errors = [], []
        def worker(index):
            try:
                self.ingest(record(str(index)))
                results.append(len(self.source.messages(CONV, 100)))
                self.source.media(CONV, str(index))
                self.assertEqual(self.source.media_reason.value, MEDIA_REASON)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(self.source.messages(CONV, 100)), 8)

    def test_failed_attach_is_atomic_and_generation_unchanged(self):
        self.ingest(record("1"))
        before = self.source.identity()
        with self.source.request_scope():
            with patch("qq_source.open_store", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    self.source.attach("10002")
        self.assertEqual(self.source.identity(), before)
        self.assertEqual(len(self.source.messages(CONV, 10)), 1)

    def test_switch_during_batch_read_cannot_return_mixed_account_payload(self):
        self.ingest(record("1"))
        original = self.store.latest
        def switch(*args, **kwargs):
            result = original(*args, **kwargs)
            self.source.attach("10002")
            return result
        with patch.object(self.store, "latest", switch):
            with self.assertRaises(AccountChangedError):
                self.source.message_windows([CONV], expected_account=self.account)
        self.assertTrue(self.store.closed)
        self.assertTrue(self.source.identity()[1].is_relative_to(self.root))

    def test_switch_forget_and_close_release_connections(self):
        first = self.store
        second_account = self.source.attach("10002")
        self.assertTrue(first.closed)
        second = self.source._store
        self.source.forget_account(second_account)
        self.assertTrue(second.closed)
        self.source.attach(UIN)
        third = self.source._store
        self.source.close()
        self.source.close()
        self.assertTrue(third.closed)
        with self.assertRaises(RuntimeError):
            self.source.attach(UIN)

    def test_foreign_library_is_refused_without_changing_binding(self):
        with self.assertRaises(StoreError):
            self.source.attach("10002", self.store)
        self.assertEqual(self.source.identity()[0], self.account)
        self.assertFalse(self.store.closed)

    def test_source_tests_never_fall_back_to_the_checkout_data_directory(self):
        from product_profile import ROOT
        forbidden = load_product("qq").data_root(ROOT).resolve()
        with patch("qq_source.open_store", wraps=__import__("qq_source").open_store) as opening:
            self.source.attach("10002")
        self.assertEqual(opening.call_args.args[1], self.root)
        self.assertFalse(self.source.identity()[1].is_relative_to(forbidden))


class CursorAndTextTests(Library):
    def test_portrait_message_count_is_peer_only_while_conversation_total_includes_self(self):
        self.ingest(record("peer", time_ms=1000), record("self", time_ms=2000, direction="self"),
                    record("image", time_ms=3000, kind="image", text=None),
                    record("recalled", time_ms=4000, status="recalled", recall_time="7"),
                    record("system", time_ms=5000, direction="system"))
        self.assertEqual(self.source.stats(CONV)[:2], (5, 1))
        metadata = self.source.profile_metadata(CONV)
        self.assertEqual((metadata["count"], metadata["textCount"]), (3, 1))
        self.assertEqual(self.source.profile_overview(CONV)["count"], 3)
        first = self.source.messages(CONV, 10)[0]["_sort"]
        self.assertEqual(self.source.profile_overview(CONV, highwater=first)["count"], 1)

    def test_frozen_highwater_and_internal_tuple_resume(self):
        self.ingest(record("1", time_ms=1000), record("2", time_ms=2000))
        high = self.source.history_highwater(CONV)
        self.ingest(record("3", time_ms=3000))
        first, cursor = self.source.history_page(CONV, high, page_size=1)
        self.assertEqual([row["id"] for row in first], ["1"])
        self.assertEqual(cursor, (1000, "qq:" + CONV, 1))
        tail, cursor = self.source.history_page(CONV, high, cursor, page_size=10)
        self.assertEqual([row["id"] for row in tail], ["2"])
        self.assertIsInstance(cursor, tuple)
        self.assertEqual(self.source.history_page(CONV, high, cursor), ([], None))
        self.assertEqual(decode_cursor(first[0]["historyCursor"], self.account, CONV), first[0]["_sort"])

    def test_foreign_highwater_before_and_reference_are_refused(self):
        self.ingest(record("1"))
        foreign = (1700000000001, "qq:u:foreign", 1)
        with self.assertRaises(ValueError):
            self.source.history_page(CONV, foreign)
        with self.assertRaises(ValueError):
            self.source.preceding_text_context(CONV, foreign)
        with self.assertRaises(ValueError):
            self.source.texts_for_refs(CONV, [(foreign[1], 1, "1")])

    def test_every_analysis_input_excludes_unusable_rows_but_keeps_valid_quote_body(self):
        values = [record("normal"), record("recalled", status="recalled", recall_time="5"),
                  record("conflict", status="conflict"), record("system", direction="system"),
                  record("unknown", kind="unknown"), record("blank", text=" \t\u3000"),
                  record("quote", text="自己的回复", quote="他人的引用"),
                  record("direction", direction="conflict")]
        self.ingest(*values)
        recent = self.source.messages(CONV, 100)
        history, _ = self.source.history_page(CONV, self.source.history_highwater(CONV))
        for items in (recent, history):
            self.assertEqual([row["id"] for row in BatchEngine.text_items(items)], ["normal", "quote"])
            self.assertNotIn("他人的引用", [row["text"] for row in items])
        context = self.source.preceding_text_context(CONV, (1700000000001, "qq:" + CONV, 100), limit=100)
        self.assertEqual([row["id"] for row in context], ["normal", "quote"])
        refs = [("qq:" + CONV, i + 1, value["native_id"]) for i, value in enumerate(values)]
        self.assertEqual(self.source.texts_for_refs(CONV, refs), ["在吗", "自己的回复"])
        self.assertEqual(self.source.stats(CONV)[1], 2)
        quotes, _ = self.source.quoted_history_page(CONV, self.source.history_highwater(CONV))
        self.assertEqual([row["id"] for row in quotes], ["quote"])

    def test_all_filtered_page_retains_its_scan_position(self):
        self.ingest(record("system", direction="system"), record("ok", time_ms=1700000000001))
        page, cursor = self.source.history_page(CONV, self.source.history_highwater(CONV), page_size=1)
        self.assertEqual(page, [])
        self.assertIsInstance(cursor, tuple)
        page, _ = self.source.history_page(CONV, self.source.history_highwater(CONV), cursor, page_size=1)
        self.assertEqual(page[0]["id"], "ok")

    def test_actual_batch_engine_resumes_long_text_after_restart_over_sparse_keys(self):
        self.ingest(record("1", text="abcdefghijABCDEFGHIJ0123456789"), record("2", text="tail"),
                    record("3", direction="system"))
        with self.store.transaction() as cursor:
            cursor.execute("UPDATE messages SET local_seq=local_seq*10+1")
        result_store = ResultStore(self.root / "results.sqlite")
        consumed = []
        def analyze_batch(session, payload, context):
            item = payload[0]
            start = item["offset"]
            end = min(start + 10, len(item["text"]))
            consumed.append((item["id"], start, end))
            return {"consumed": [{"start": start, "end": end, "complete": end == len(item["text"])}],
                    "result": {"emotion": [{"label": "平静", "probability": 1}],
                               "intent": [], "intentBroad": [], "score": .5}, "durationMs": 0}
        backend = SimpleNamespace(source=self.source, analyzer=SimpleNamespace(analyze_batch=analyze_batch),
                                  jobs_lock=threading.Lock(), performance={},
                                  _legacy_profile_state=lambda *args: empty_state(),
                                  _take_recent_priority=lambda key: None,
                                  _assert_scope=lambda scope: self.assertEqual(scope, self.source.identity()))
        args = (self.account, CONV, "synthetic-v1", result_store, {}, self.source.identity(), "key")
        first_engine = BatchEngine(backend)
        iterator = first_engine.incremental(*args)
        next(iterator)  # Interrupt after the first ten characters, then use a fresh engine.
        iterator.close()
        engine = BatchEngine(backend)
        resumed = engine.incremental(*args)
        for _ in range(10):
            try:
                next(resumed)
            except StopIteration:
                break
        else:
            self.fail("batch engine did not complete within the synthetic bound")
        self.assertEqual(consumed, [("1", 0, 10), ("1", 10, 20), ("1", 20, 30), ("2", 0, 4)])
        saved = engine.snapshot(self.account, CONV, "synthetic-v1", result_store)
        self.assertTrue(saved["complete"])
        self.assertEqual(saved["charOffset"], 0)
        self.assertEqual(saved["state"]["count"], 2)
        self.assertEqual(saved["cursor"], self.source.history_highwater(CONV))


class RecoveryTests(Library):
    def test_foreign_account_backup_and_same_version_wrong_schema_are_refused(self):
        self.ingest(record("1"))
        before = self.state()
        foreign = QQMessageStore(self.root / "foreign.sqlite")
        self.addCleanup(foreign.close)
        foreign.ensure_conversation(account_key("10002"), CONV)
        backup = foreign.backup_to(self.root / "foreign-backup.sqlite")
        with self.assertRaisesRegex(StoreError, "another account"):
            self.store.restore_from(backup)
        with closing(sqlite3.connect(backup)) as connection, connection:
            connection.execute("DROP INDEX idx_messages_conv_order")
        with self.assertRaisesRegex(StoreError, "schema differs"):
            self.store.restore_from(backup)
        self.assertEqual(self.state(), before)

    def test_valid_backup_restores_contents_and_invalid_backup_preserves_old_library(self):
        self.ingest(record("1"))
        backup = self.store.backup_to(self.root / "backup.sqlite")
        self.ingest(record("2"))
        self.store.restore_from(backup)
        self.assertEqual([row["native_id"] for row in self.store.page(self.account, CONV)[0]], ["1"])
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.restore_from(broken)
        self.assertEqual(len(self.source.messages(CONV, 10)), 1)

    def test_replace_failure_reopens_the_unchanged_original(self):
        self.ingest(record("1"))
        backup = self.store.backup_to(self.root / "backup.sqlite")
        self.ingest(record("2"))
        before = self.state()
        with patch("qq_message_store.os.replace", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                self.store.restore_from(backup)
        self.assertEqual(self.state(), before)

    def test_v2_upgrade_keeps_rows_and_saves_a_recoverable_pre_upgrade_copy(self):
        self.ingest(record("1"))
        original = self.store.path
        self.store.close()
        # Build the actual legacy layout, rather than marking a v4 database as v2.
        from qq_message_store import SCHEMA_SQL_V3, _statements
        path = self.root / "legacy-v2.sqlite"
        with closing(sqlite3.connect(path)) as conn, conn:
            for statement in _statements(SCHEMA_SQL_V3):
                if statement.startswith("CREATE TABLE message_observations"):continue
                conn.execute(statement)
            conn.execute("ATTACH DATABASE ? AS original",(str(original),))
            tables=[row[0] for row in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table'")]
            for table in tables:
                columns=','.join(row[1] for row in conn.execute('PRAGMA main.table_info('+table+')'))
                conn.execute('INSERT INTO main.'+table+' ('+columns+') SELECT '+columns+' FROM original.'+table)
            conn.execute("PRAGMA user_version=2")
        reopened = QQMessageStore(path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.ingest(self.account, CONV, [record("1")])["unchanged"], 1)
        backups=list(path.parent.glob(path.name+".v2-backup-*"))
        self.assertEqual(len(backups),1,"the original v2 recovery copy must survive both migration steps")
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

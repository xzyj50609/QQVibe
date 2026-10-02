"""T05 message library checks against a real SQLite file in a throwaway directory.

Nothing here touches the user's installed client: every library lives under a
temporary product data root (09 section 1, "只在临时数据目录验证").
"""
from __future__ import annotations

import json
import re
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import qq_message_store as store
from product_profile import load_product
from qq_identity import account_key, canonical_uin

UIN = "10001"
CONV = "u:u_peer_synth01"


def record(native_id, *, time_ms=1_700_000_000_000, text="在吗", direction="peer",
           kind="text", status="normal", recall_time="0", native_seq="9223372036854775808",
           msg_type=2, quote=None, sender_uin="179000000001"):
    body = {"msgId": native_id, "msgTime": str(time_ms // 1000), "peerUid": "u_peer_synth01",
            "senderUin": sender_uin, "text": text}
    return {"native_id_kind": "qce-msgId", "native_id": native_id,
            "native_seq": native_seq,
            "conversation_key": CONV, "sender_uin": sender_uin, "sender_uid": "u_peer_synth01",
            "send_type": "0", "direction": direction, "time_ms": time_ms, "msg_type": msg_type,
            "kind": kind, "text": text, "quote": quote, "status": status,
            "recall_time": recall_time, "raw": json.dumps(body, ensure_ascii=False),
            "normalize_version": "qq-v1"}


CHECKPOINT = {"cursor_version": 1, "window_start_ms": 1_699_999_998_000,
              "window_end_ms": 1_700_000_000_000, "scanned_through_ms": 1_700_000_000_000,
              "last_commit_time_ms": 1, "last_commit_count": 1, "last_task_status": "complete",
              "attempts": 0, "overlap_ms": 2000, "state": "COMMITTED", "last_error": None,
              "partial_reason": None}


class Library(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="qqvibe-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = account_key(UIN)
        opened = store.QQMessageStore(store.database_path(self.account, self.root))
        self.addCleanup(opened.close)
        self.db = opened
        self.db.ensure_conversation(self.account, CONV, peer_uid="u_peer_synth01")

    def rows(self, sql, values=()):
        return self.db.connection.execute(sql, tuple(values)).fetchall()

    def sequences(self):
        return [row["local_seq"] for row in
                self.rows("SELECT local_seq FROM messages ORDER BY time_ms, local_seq")]

    def revision(self):
        return self.db.revision(self.account, CONV)


class IdentityTests(Library):
    def test_the_owner_anchor_is_the_canonical_uin_not_the_uid(self):
        """C03-R3: a UID appearing later must not open a second library."""
        self.assertEqual(canonical_uin(" 0075624 "), "75624")
        self.assertEqual(account_key("10001"), account_key("0010001"))
        self.assertNotEqual(account_key("10001"), account_key("10002"))
        for bad in (None, "", "abc", "0", "1.5", True, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    account_key(bad)

    def test_the_library_lives_under_the_product_data_root_in_plain_hex(self):
        path = store.database_path(self.account, self.root)
        self.assertEqual(path.parts[-3:], ("accounts", self.account[2:], "messages.sqlite"))
        self.assertEqual(path.parts[-4], "QQVibeData")
        self.assertNotIn(":", path.name)

    def test_schema_version_is_recorded_and_reopening_keeps_the_rows(self):
        self.assertEqual(self.rows("PRAGMA user_version")[0][0], store.SCHEMA_VERSION)
        self.db.ingest(self.account, CONV, [record("1")])
        self.db.close()
        reopened = store.QQMessageStore(store.database_path(self.account, self.root))
        self.addCleanup(reopened.close)
        self.db = reopened
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 1)
        self.assertEqual(self.db.integrity_errors(), [])

    def test_a_foreign_schema_version_is_refused(self):
        self.db.close()
        path = store.database_path(self.account, self.root)
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA user_version=7")
        connection.close()
        with self.assertRaises(store.StoreError):
            store.QQMessageStore(path)


class SchemaParityTests(unittest.TestCase):
    def test_the_code_schema_matches_the_documented_ddl(self):
        text = (Path(__file__).resolve().parents[1] /
                "docs/contracts/data-schema.md").read_text(encoding="utf-8")
        documented = "\n".join(re.findall(r"```sql\n(.*?)```", text, re.S))

        def normalize(statement):
            without_comments = re.sub(r"--[^\n]*", " ", statement)
            return re.sub(r"\s+", " ", without_comments).strip().rstrip(";").casefold()

        in_code = {normalize(part) for part in store._statements(store.SCHEMA_SQL_V3)}
        in_doc = {normalize(part) for part in store._statements(documented)}
        self.assertEqual(in_code - in_doc, set(), "code declares tables the contract never documented")
        self.assertEqual(in_doc - in_code, set(), "the contract documents tables the code never creates")

    def test_the_extended_schema_matches_r2_ddl(self):
        text = (Path(__file__).resolve().parents[1] / "docs/contracts/group-data-schema.md").read_text(encoding="utf-8")
        documented = '\n'.join(re.findall(r'```sql\n(.*?)```',text,re.S))
        normalize = lambda value: re.sub(r'\s+',' ',value).strip().rstrip(';').casefold()
        self.assertEqual({normalize(value) for value in store._statements(documented)},
                         {normalize(value) for value in store._statements(store.SCHEMA_SQL)})


class IngestTests(Library):
    def test_first_batch_keeps_input_order_and_does_not_claim_backfill(self):
        """QCE hands pages newest-first; that is an initial fill, not a late arrival."""
        newest_first = [record("3", time_ms=1_700_000_020_000), record("2", time_ms=1_700_000_010_000),
                        record("1", time_ms=1_700_000_000_000)]
        outcome = self.db.ingest(self.account, CONV, newest_first)
        self.assertEqual(outcome["inserted"], 3)
        self.assertEqual(outcome["backfilled"], 0)
        self.assertEqual(self.revision(), (1, None, None))
        # Local keys follow the order the source delivered, canonical order sorts by time.
        self.assertEqual([row["native_id"] for row in
                          self.rows("SELECT native_id FROM messages ORDER BY local_seq")],
                         ["3", "2", "1"])
        self.assertEqual(self.sequences(), [3, 2, 1])

    def test_same_second_duplicate_text_keeps_both_rows(self):
        """X1: two '好' in one second are two messages, not one with a duplicate hash."""
        self.db.ingest(self.account, CONV, [record("11", text="好"), record("12", text="好")])
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 2)
        self.db.ingest(self.account, CONV, [record("11", text="好"), record("12", text="好")])
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 2)

    def test_reingesting_the_whole_batch_changes_nothing(self):
        batch = [record("11"), record("12")]
        self.db.ingest(self.account, CONV, batch)
        before = self.revision()
        outcome = self.db.ingest(self.account, CONV, batch)
        self.assertEqual(outcome["inserted"], 0)
        self.assertEqual(outcome["unchanged"], 2)
        self.assertEqual(self.revision(), before)

    def test_big_native_sequences_round_trip_as_text(self):
        """X4/RS06/V03: 2**63 must survive storage character for character."""
        original = "9223372036854775808"
        self.db.ingest(self.account, CONV, [record("1", native_seq=original)])
        row = self.rows("SELECT native_seq, typeof(native_seq) AS t FROM messages")[0]
        self.assertEqual((row["native_seq"], row["t"]), (original, "text"))

    def test_an_integer_native_seq_is_rejected_not_coerced(self):
        with self.assertRaises(store.StoreError):
            self.db.ingest(self.account, CONV, [record("1", native_seq=7)])

    def test_a_record_from_another_conversation_cannot_be_mixed_in(self):
        smuggled = dict(record("1"), conversation_key="u:someone_else")
        with self.assertRaises(store.StoreError):
            self.db.ingest(self.account, CONV, [smuggled])

    def test_unknown_columns_are_a_programming_error(self):
        with self.assertRaises(store.StoreError):
            self.db.ingest(self.account, CONV, [dict(record("1"), wxid="wxid_nope")])

    def test_an_unregistered_conversation_is_refused(self):
        with self.assertRaises(store.StoreError):
            self.db.ingest(self.account, "u:not-registered", [record("1")])


class BackfillTests(Library):
    def setUp(self):
        super().setUp()
        self.db.ingest(self.account, CONV, [record("20", time_ms=1_700_000_020_000),
                                            record("10", time_ms=1_700_000_010_000)])

    def test_older_backfill_keeps_existing_keys_and_uses_the_full_position(self):
        """X5/C07-R2: local_seq never moves; the boundary is a (time, seq) pair."""
        before = {row["native_id"]: row["local_seq"] for row in
                  self.rows("SELECT native_id, local_seq FROM messages")}
        outcome = self.db.ingest(self.account, CONV, [record("1", time_ms=1_700_000_001_000),
                                                      record("2", time_ms=1_700_000_002_000)])
        after = {row["native_id"]: row["local_seq"] for row in
                 self.rows("SELECT native_id, local_seq FROM messages")}
        self.assertEqual(outcome["backfilled"], 2)
        self.assertEqual({key: after[key] for key in before}, before)
        # The two late arrivals took the next free keys (3, 4) and still sort to the front,
        # because canonical order is (time_ms, local_seq), not "sequence means recency".
        self.assertEqual(self.sequences(), [3, 4, 2, 1])
        revision, earliest_time, earliest_seq = self.revision()
        self.assertEqual(revision, 2)
        self.assertEqual((earliest_time, earliest_seq), (1_700_000_001_000, 3))

    def test_the_boundary_is_a_pair_not_the_smallest_sequence(self):
        """A late-arriving old message has the earliest time and the largest local key."""
        self.db.ingest(self.account, CONV, [record("1", time_ms=1_700_000_001_000)])
        _revision, earliest_time, earliest_seq = self.revision()
        self.assertLess(earliest_time, 1_700_000_010_000)
        self.assertGreater(earliest_seq, 1)

    def test_an_append_after_the_covered_range_does_not_invalidate_anything(self):
        outcome = self.db.ingest(self.account, CONV, [record("30", time_ms=1_700_000_030_000)])
        self.assertEqual(outcome["backfilled"], 0)
        self.assertEqual(self.revision()[1], None)

    def test_first_and_last_time_window_follows_the_stored_rows(self):
        self.db.ingest(self.account, CONV, [record("5", time_ms=1_700_000_005_000)])
        row = self.rows("SELECT first_time_ms, last_time_ms FROM conversations")[0]
        self.assertEqual((row["first_time_ms"], row["last_time_ms"]),
                         (1_700_000_005_000, 1_700_000_020_000))


class StatusTransitionTests(Library):
    def test_a_recall_updates_the_row_instead_of_adding_one(self):
        """X6: same id, recall arrives later; the body stays and the row is not duplicated."""
        self.db.ingest(self.account, CONV, [record("1", text="原文")])
        outcome = self.db.ingest(self.account, CONV, [record("1", text="原文", status="recalled",
                                                             recall_time="1700000123")])
        self.assertEqual(outcome["recalled"], 1)
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 1)
        row = self.rows("SELECT status, text, recall_time FROM messages")[0]
        self.assertEqual((row["status"], row["text"], row["recall_time"]),
                         ("recalled", "原文", "1700000123"))
        self.assertEqual(self.revision()[0], 2)

    def test_a_different_direction_for_the_same_id_is_a_conflict_with_both_sides_kept(self):
        self.db.ingest(self.account, CONV, [record("1", direction="peer", text="原文")])
        outcome = self.db.ingest(self.account, CONV, [record("1", direction="self", text="原文")])
        self.assertEqual(outcome["conflicts"], 1)
        row = self.rows("SELECT status, text, direction FROM messages")[0]
        self.assertEqual((row["status"], row["text"], row["direction"]),
                         ("conflict", "原文", "peer"), "the stored claim is not overwritten")
        conflict = self.rows("SELECT incoming_raw FROM message_conflicts")[0]
        self.assertIn('"msgId": "1"', conflict["incoming_raw"])

    def test_a_competing_body_is_quarantined_without_losing_the_original(self):
        self.db.ingest(self.account, CONV, [record("1", text="周六?")])
        outcome = self.db.ingest(self.account, CONV, [record("1", text="周六见?")])
        self.assertEqual(outcome["conflicts"], 1)
        self.assertEqual(self.rows("SELECT status FROM messages")[0]["status"], "conflict")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM message_conflicts")[0]["n"], 1)

    def test_identical_replay_of_a_recalled_row_is_still_idempotent(self):
        self.db.ingest(self.account, CONV, [record("1", status="recalled", recall_time="5")])
        outcome = self.db.ingest(self.account, CONV, [record("1", status="recalled", recall_time="5")])
        self.assertEqual((outcome["unchanged"], outcome["recalled"]), (1, 0))


class TransactionTests(Library):
    def test_a_failed_checkpoint_rolls_the_messages_back_too(self):
        """V11/R17: the cursor can never be committed ahead of the rows it claims."""
        broken = dict(CHECKPOINT, state="NOT-A-STATE")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.ingest(self.account, CONV, [record("1")], checkpoint=broken)
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 0)
        self.assertEqual(len(self.rows("SELECT 1 FROM sync_checkpoints")), 0)

    def test_a_checkpoint_and_its_messages_commit_together(self):
        self.db.ingest(self.account, CONV, [record("1")], checkpoint=CHECKPOINT)
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 1)
        coverage = self.db.coverage(self.account, CONV)
        self.assertEqual((coverage["covered"], coverage["state"], coverage["scannedThroughMs"]),
                         (True, "COMMITTED", 1_700_000_000_000))

    def test_a_partial_task_leaves_the_frontier_invisible_as_complete(self):
        partial = dict(CHECKPOINT, state="PARTIAL", last_task_status="partial",
                       scanned_through_ms=0, partial_reason="budget", attempts=1)
        self.db.ingest(self.account, CONV, [record("1")], checkpoint=partial)
        coverage = self.db.coverage(self.account, CONV)
        self.assertEqual((coverage["covered"], coverage["partialReason"], coverage["attempts"]),
                         (False, "budget", 1))

    def test_an_unknown_checkpoint_field_is_refused_before_any_write(self):
        with self.assertRaises(store.StoreError):
            self.db.ingest(self.account, CONV, [record("1")],
                           checkpoint=dict(CHECKPOINT, last_commit_seq="7"))
        self.assertEqual(len(self.rows("SELECT 1 FROM messages")), 0)

    def test_the_platform_column_is_not_overwritable_by_callers(self):
        with self.assertRaises(store.StoreError):
            self.db.ingest(self.account, CONV, [record("1")],
                           checkpoint=dict(CHECKPOINT, platform="wechat"))

    def test_an_uncommitted_transaction_leaves_no_partial_row(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as cursor:
                cursor.execute("INSERT INTO messages(message_key, account_key, conversation_key,"
                               " native_id_kind, native_id, local_seq, direction, time_ms, kind,"
                               " normalize_version) VALUES ('m:half',?,?, 'qce-msgId','half',9,"
                               "'peer',1,'text','qq-v1')", (self.account, "u:none"))


class PagingTests(Library):
    def setUp(self):
        super().setUp()
        self.db.ingest(self.account, CONV, [record(str(index), time_ms=1_700_000_000_000 + index * 1000)
                                            for index in range(1, 6)])

    def test_every_non_empty_page_returns_a_cursor_and_the_tail_is_empty(self):
        """C02-R3: batch_engine breaks on None, so a short final page must still carry one."""
        collected, cursor, tail_cursor = [], None, None
        for _ in range(10):
            page, cursor = self.db.page(self.account, CONV, cursor, page_size=2)
            collected += [row["native_id"] for row in page]
            if len(page) < 2:
                tail_cursor = cursor
                break
        self.assertEqual(collected, ["1", "2", "3", "4", "5"])
        self.assertIsNotNone(tail_cursor, "the three-row tail still needs a cursor")
        self.assertEqual(self.db.page(self.account, CONV, tail_cursor, page_size=2), ([], None))

    def test_the_same_second_pair_is_ordered_by_the_local_key(self):
        self.db.ingest(self.account, CONV, [record("a", time_ms=5), record("b", time_ms=5)])
        rows, _cursor = self.db.page(self.account, CONV, None, page_size=5)
        self.assertEqual([row["native_id"] for row in rows if row["time_ms"] == 5], ["a", "b"])

    def test_a_cursor_from_another_conversation_is_rejected(self):
        with self.assertRaises(ValueError):
            self.db.page(self.account, CONV, (1_700_000_001_000, "qq:u:other", 1), page_size=2)

    def test_highwater_is_the_batch_cursor_shape(self):
        highwater = self.db.highwater(self.account, CONV)
        self.assertEqual(highwater[1], "qq:" + CONV)
        self.assertEqual((highwater[0], highwater[2]), (1_700_000_005_000, 5))

    def test_highwater_of_an_empty_conversation_is_none(self):
        self.db.ensure_conversation(self.account, "u:u_empty", peer_uid="u_empty")
        self.assertIsNone(self.db.highwater(self.account, "u:u_empty"))

    def test_coverage_for_an_unregistered_conversation_is_a_plain_no(self):
        self.assertEqual(self.db.coverage(self.account, "u:unknown")["covered"], False)

    def test_texts_for_refs_skips_missing_ones_instead_of_failing(self):
        found = self.db.texts_for_refs(self.account, CONV, [("qq:" + CONV, 1, "1"), ("qq:" + CONV, 999, "nope")])
        self.assertEqual(found, ["在吗"])


class AliasTests(Library):
    def test_only_a_confirmed_alias_can_ever_merge_identities(self):
        """V04: pending evidence is recorded but never joins anyone."""
        self.db.record_alias(self.account, uin="10002", uid="u_x", alias_kind="qce-friends",
                             evidence="GET /api/friends at 1", confidence="pending", created_at=1)
        self.db.record_alias(self.account, uin="10002", uid="u_x", alias_kind="qce-friends",
                             evidence="GET /api/friends at 2", confidence="pending", created_at=2)
        self.assertEqual(len(self.rows("SELECT 1 FROM identity_aliases")), 1)
        with self.assertRaises(ValueError):
            self.db.record_alias(self.account, uin="10002", alias_kind="guess",
                                 confidence="certain", created_at=1, evidence="e")

    def test_empty_sentinels_keep_the_unique_key_enforceable(self):
        self.db.record_alias(self.account, uin="10002", uid="u_x", alias_kind="self-info",
                             evidence="e", confidence="confirmed", created_at=1)
        row = self.rows("SELECT uid, peer_uid FROM identity_aliases")[0]
        self.assertEqual(row["peer_uid"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

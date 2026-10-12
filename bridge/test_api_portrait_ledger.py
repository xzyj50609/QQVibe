"""API evidence ledger checks using only synthetic SQLite and classification values."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from api_portrait_ledger import ApiPortraitLedger, statistics_from_records
from api_portrait_statistics import append_batch, empty_statistics, profile_from_statistics
from result_store import ResultStore
from test_api_portrait_statistics import piece, signal


class ApiPortraitLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ResultStore(Path(temporary.name) / "synthetic.sqlite3")
        self.ledger = ApiPortraitLedger(self.store)
        self.scope = self.ledger.scope("synthetic-account", "friend", "synthetic-api", "friend", "rules")
        with self.store.connect() as connection:
            self.ledger.begin(connection, self.scope)
        self.stats = empty_statistics("rules")

    def commit(self, pieces, result=None, *, selected=None, group=False, local_subject="friend", ready=None):
        selected = selected or self.scope
        prepared = self.ledger.prepare(self.stats, result, pieces, group=group,
                                        local_subject=local_subject, selected=selected)
        updated = self.ledger.statistics(selected, prepared)
        prepared["statistics"] = updated
        with self.store.connect() as connection:
            self.ledger.persist(connection, selected, prepared, ready=ready)
        self.stats = updated
        return prepared

    def assert_close(self, left, right):
        if isinstance(left, dict):
            self.assertEqual(set(left), set(right))
            for key in left:
                self.assert_close(left[key], right[key])
        elif isinstance(left, list):
            self.assertEqual(len(left), len(right))
            for first, second in zip(left, right):
                self.assert_close(first, second)
        elif type(left) is float:
            self.assertAlmostEqual(left, right, places=9)
        else:
            self.assertEqual(left, right)

    def test_append_only_batches_match_established_local_api_statistics(self):
        original = empty_statistics("rules")
        for start, end, judgment in ((1, 121, signal()), (121, 201, signal(left=.1, score=-.4))):
            pieces = [piece(index) for index in range(start, end)]
            original = append_batch(original, judgment, pieces, subject="friend")
            self.commit(pieces, judgment)
            self.assert_close(self.stats, original)
            self.assert_close(profile_from_statistics(self.stats, "rules", subject="friend"),
                              profile_from_statistics(original, "rules", subject="friend"))
        self.assertEqual(len(self.ledger.known_ids(self.scope)), 200)

    def test_inserted_old_message_replays_true_rank_without_reclassifying_saved_rows(self):
        self.commit([piece(1)], signal(score=1))
        self.commit([piece(3)], signal(score=-1))
        self.commit([piece(2)], signal(score=.2), ready=True)
        ordered = empty_statistics("rules")
        for index, score in ((1, 1), (2, .2), (3, -1)):
            ordered = append_batch(ordered, signal(score=score), [piece(index)], subject="friend")
        self.assert_close(self.stats, ordered)
        self.assertEqual(self.stats["state"]["scoreWeighted"], .2 - 2)
        self.assertEqual(self.stats["state"]["latest"][-1], "m3")
        self.assertEqual(self.ledger.known_ids(self.scope), {"m1", "m2", "m3"})

    def test_duplicate_saved_message_is_idempotent_and_keeps_original_signal(self):
        self.commit([piece(1)], signal(score=.4))
        before = deepcopy(self.stats)
        prepared = self.commit([piece(1)], signal(score=-.9))
        self.assertEqual(prepared["records"], [])
        self.assertEqual(prepared["batchIncrement"], 0)
        self.assertEqual(self.stats, before)
        self.assertEqual(len(self.ledger.records(self.scope)), 1)

    def test_same_batch_exact_duplicate_does_not_double_count(self):
        self.commit([piece(1), piece(1)], signal())
        self.assertEqual(self.stats["state"]["targetCount"], 1)
        self.assertEqual(self.stats["state"]["batchCount"], 1)

    def test_target_fragments_match_length_weighting_and_complete_keyword_collection(self):
        first = [piece(1, "engin", complete=False)]
        last = [piece(1, "eering", part=1), piece(2)]
        before = append_batch(empty_statistics("rules"), signal(score=.8), first, subject="friend")
        self.commit(first, signal(score=.8))
        self.assertEqual(self.ledger.known_ids(self.scope), set())
        self.assert_close(self.stats, before)
        after = append_batch(before, signal(score=-.2), last, subject="friend")
        self.commit(last, signal(score=-.2), ready=True)
        self.assert_close(self.stats, after)
        self.assertIn("engineering", self.stats["state"]["words"])
        self.assertEqual(self.ledger.known_ids(self.scope), {"m1", "m2"})

    def test_backfilled_fragment_pending_preserves_real_latest_and_completes(self):
        self.commit([piece(5)], signal(score=.5))
        self.commit([piece(1, "engin", complete=False)], signal(score=.1))
        self.assertEqual(self.stats["state"]["latest"][-1], "m5")
        self.assertEqual(self.stats["pending"]["messageId"], "m1")
        self.commit([piece(1, "eering", part=1)], signal(score=.3), ready=True)
        self.assertEqual(self.stats["state"]["latest"][-1], "m5")
        self.assertEqual(self.stats["state"]["targetCount"], 2)
        self.assertIsNone(self.stats["pending"])

    def test_non_target_fragments_are_covered_only_after_contiguous_last_piece(self):
        self.commit([piece(1, "first", target=False, sender="SELF", speaker="self", complete=False)], None)
        self.assertEqual(self.ledger.known_ids(self.scope), set())
        broken = [piece(1, "third", target=False, sender="SELF", speaker="self", part=2)]
        with self.assertRaises(ValueError):
            self.ledger.prepare(self.stats, None, broken, local_subject="friend", selected=self.scope)
        self.commit([piece(1, "last", target=False, sender="SELF", speaker="self", part=1)], None, ready=True)
        self.assertEqual(self.ledger.known_ids(self.scope), {"m1"})
        self.assertEqual(self.stats["state"]["count"], 0)

    def test_group_self_count_and_member_scope_match_local_rules(self):
        selected = self.ledger.scope("synthetic-account", "room", "synthetic-api", "room", "rules")
        with self.store.connect() as connection:
            self.ledger.begin(connection, selected)
        records = [piece(1, target=False, sender="SELF", speaker="self"), piece(2)]
        expected = append_batch(empty_statistics("rules"), signal(), records, is_group=True, subject="")
        self.commit(records, signal(), selected=selected, group=True, local_subject="", ready=True)
        self.assert_close(self.stats, expected)
        self.assertEqual(self.stats["state"]["count"], 2)
        self.assertEqual(self.stats["state"]["targetCount"], 1)
        self.assertEqual(self.ledger.known_ids(selected), {"m1", "m2"})

    def test_invalid_piece_identity_or_noncontiguous_duplicate_is_not_accepted(self):
        self.commit([piece(1)], signal())
        wrong = deepcopy(piece(1))
        wrong["speaker"] = "different person"
        with self.assertRaises(ValueError):
            self.ledger.prepare(self.stats, signal(), [wrong], local_subject="friend", selected=self.scope)
        with self.assertRaises(ValueError):
            self.ledger.prepare(self.stats, signal(), [piece(1, part=1)], local_subject="friend", selected=self.scope)
        with self.assertRaises(ValueError):
            self.ledger.prepare(self.stats, signal(), [piece(3), piece(2)], local_subject="friend", selected=self.scope)

    def test_uninitialized_legacy_aggregate_requires_explicit_new_baseline(self):
        missing = self.ledger.scope("synthetic-account", "legacy", "synthetic-api", "legacy", "rules")
        legacy = append_batch(empty_statistics("rules"), signal(), [piece(1)], subject="legacy")
        self.assertFalse(self.ledger.metadata(missing)["initialized"])
        with self.assertRaisesRegex(ValueError, "new evidence baseline"):
            self.ledger.prepare(legacy, signal(), [piece(2)], local_subject="legacy", selected=missing)
        with self.store.connect() as connection:
            self.ledger.begin(connection, missing)
        self.assertTrue(self.ledger.metadata(missing)["initialized"])
        self.assertFalse(self.ledger.metadata(missing)["ready"])

    def test_checkpoint_connection_rollback_keeps_coverage_and_metadata_atomic(self):
        prepared = self.ledger.prepare(self.stats, signal(), [piece(1)], local_subject="friend", selected=self.scope)
        before = self.ledger.metadata(self.scope)
        with self.assertRaisesRegex(RuntimeError, "synthetic checkpoint failure"):
            with self.store.connect() as connection:
                self.ledger.persist(connection, self.scope, prepared, ready=True, revision="revision", prefix_signature="prefix",
                                    highwater=[1, "message__message_0.db", 1])
                raise RuntimeError("synthetic checkpoint failure")
        self.assertEqual(self.ledger.known_ids(self.scope), set())
        self.assertEqual(self.ledger.metadata(self.scope), before)

    def test_partial_baseline_survives_restart_without_forcing_rebuild(self):
        self.commit([piece(1)], signal())
        restarted = ApiPortraitLedger(self.store)
        metadata = restarted.metadata(self.scope)
        self.assertTrue(metadata["initialized"])
        self.assertFalse(metadata["ready"])
        self.assertEqual(restarted.known_ids(self.scope), {"m1"})
        self.assert_close(restarted.statistics(self.scope), self.stats)

    def test_stale_prepared_generation_cannot_commit_twice(self):
        prepared = self.ledger.prepare(self.stats, signal(), [piece(1)], local_subject="friend", selected=self.scope)
        with self.store.connect() as connection:
            self.ledger.persist(connection, self.scope, prepared)
        with self.assertRaisesRegex(ValueError, "changed before checkpoint"):
            with self.store.connect() as connection:
                self.ledger.persist(connection, self.scope, prepared)
        self.assertEqual(self.ledger.known_ids(self.scope), {"m1"})

    def test_metadata_completion_and_clear_are_source_and_subject_scoped(self):
        self.commit([piece(1)], signal(), ready=True)
        other = self.ledger.scope("other-account", "friend", "synthetic-api", "friend", "rules")
        with self.store.connect() as connection:
            self.ledger.begin(connection, other)
            self.ledger.persist(connection, self.scope, revision="revision1", prefix_signature="prefix1",
                                highwater=[5, "message__message_0.db", 5], ready=True)
        metadata = self.ledger.metadata(self.scope)
        self.assertEqual((metadata["revision"], metadata["prefixSignature"], metadata["highwater"]),
                         ("revision1", "prefix1", [5, "message__message_0.db", 5]))
        with self.store.connect() as connection:
            self.ledger.clear(connection, account="synthetic-account", user="friend", source_id="synthetic-api")
        self.assertFalse(self.ledger.metadata(self.scope)["initialized"])
        self.assertTrue(self.ledger.metadata(other)["initialized"])
        with self.store.connect() as connection:
            with self.assertRaises(ValueError):
                self.ledger.clear(connection)

    def test_ready_cannot_hide_unfinished_fragments(self):
        self.commit([piece(1, "fragment", complete=False)], signal())
        with self.assertRaisesRegex(ValueError, "cannot be ready"):
            with self.store.connect() as connection:
                self.ledger.persist(connection, self.scope, ready=True)
        self.assertFalse(self.ledger.metadata(self.scope)["ready"])

    def test_new_unfinished_message_revokes_previous_ready_marker(self):
        self.commit([piece(1)], signal(), ready=True)
        self.assertTrue(self.ledger.metadata(self.scope)["ready"])
        self.commit([piece(2, "unfinished", complete=False)], signal())
        self.assertFalse(self.ledger.metadata(self.scope)["ready"])

    def test_wrong_subject_or_skipped_non_target_fragment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "subject scope changed"):
            self.ledger.prepare(self.stats, signal(), [piece(1)], local_subject="other-person", selected=self.scope)
        self.commit([piece(1, "first", target=False, sender="SELF", speaker="self", complete=False)], None)
        with self.assertRaisesRegex(ValueError, "fragment skipped"):
            self.ledger.prepare(self.stats, signal(), [piece(2)], local_subject="friend", selected=self.scope)

    def test_initialized_empty_ledger_cannot_hide_nonempty_legacy_statistics(self):
        legacy = append_batch(empty_statistics("rules"), signal(), [piece(1)], subject="friend")
        with self.assertRaisesRegex(ValueError, "statistics do not match"):
            self.ledger.prepare(legacy, signal(), [piece(2)], local_subject="friend", selected=self.scope)

    def test_static_clear_is_safe_on_old_store_without_ledger_tables(self):
        from api_portrait_ledger import clear_ledger
        connection = sqlite3.connect(":memory:")
        try:
            clear_ledger(connection, account="synthetic-account", source_id="synthetic-api")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        finally:
            connection.close()

    def test_regular_tail_prepare_and_checkpoint_never_load_or_replay_all_rows(self):
        with patch.object(self.ledger, "_rows", side_effect=AssertionError("full ledger read")), \
                patch("api_portrait_ledger.statistics_from_records", side_effect=AssertionError("full statistical replay")):
            prepared = self.ledger.prepare(self.stats, signal(), [piece(1), piece(2)],
                                            local_subject="friend", selected=self.scope)
            self.assertTrue(prepared["appendOnly"])
            self.stats = append_batch(self.stats, signal(), [piece(1), piece(2)], subject="friend")
            prepared["statistics"] = self.stats
            with self.store.connect() as connection:
                self.ledger.persist(connection, self.scope, prepared)
            next_prepared = self.ledger.prepare(self.stats, signal(score=-.2), [piece(3)],
                                                 local_subject="friend", selected=self.scope)
            self.assertTrue(next_prepared["appendOnly"])
            next_prepared["statistics"] = append_batch(self.stats, signal(score=-.2), [piece(3)], subject="friend")
            with self.store.connect() as connection:
                self.ledger.persist(connection, self.scope, next_prepared)

    def test_state_hash_rejects_changed_weights_even_when_counts_match(self):
        self.commit([piece(1)], signal())
        corrupted = deepcopy(self.stats)
        corrupted["state"]["scoreSum"] = -.25
        with self.assertRaisesRegex(ValueError, "statistics do not match"):
            self.ledger.prepare(corrupted, signal(), [piece(2)], local_subject="friend", selected=self.scope)

    def test_old_ledger_without_state_hash_replays_once_then_uses_checkpoint_hash(self):
        self.commit([piece(1)], signal())
        with self.store.connect() as connection:
            connection.execute("UPDATE api_portrait_ledger_meta_v1 SET state_hash=NULL")
        import api_portrait_ledger
        with patch("api_portrait_ledger.statistics_from_records", wraps=api_portrait_ledger.statistics_from_records) as replay:
            prepared = self.ledger.prepare(self.stats, signal(), [piece(2)], local_subject="friend", selected=self.scope)
            self.assertEqual(replay.call_count, 1)
            prepared["statistics"] = append_batch(self.stats, signal(), [piece(2)], subject="friend")
            with self.store.connect() as connection:
                self.ledger.persist(connection, self.scope, prepared)
            self.ledger.prepare(prepared["statistics"], signal(), [piece(3)], local_subject="friend", selected=self.scope)
            self.assertEqual(replay.call_count, 1)

    def test_batch_id_lookups_are_chunked_and_old_insert_requires_slow_path(self):
        self.commit([piece(2000)], signal())
        traces = []
        original_connect = self.store.connect
        from contextlib import contextmanager
        @contextmanager
        def traced_connect():
            with original_connect() as connection:
                connection.set_trace_callback(traces.append)
                yield connection
        with patch.object(self.store, "connect", traced_connect):
            prepared = self.ledger.prepare(self.stats, signal(), [piece(index) for index in range(1, 1002)],
                                            local_subject="friend", selected=self.scope)
        lookups = [query for query in traces if "message_id IN (" in query]
        self.assertEqual(len(lookups), 3)
        self.assertFalse(prepared["appendOnly"])
        duplicate = self.ledger.prepare(self.stats, signal(), [piece(2000)], local_subject="friend", selected=self.scope)
        self.assertFalse(duplicate["appendOnly"])

    def test_raw_source_clear_removes_only_that_sources_scoped_variants(self):
        from api_portrait_ledger import clear_ledger
        raw = "a" * 32
        one = self.ledger.scope("synthetic-account", "friend", raw + ":portrait-v1", "friend", "rules")
        two = self.ledger.scope("synthetic-account", "friend", raw + ":portrait-v2", "friend", "rules")
        unrelated = self.ledger.scope("synthetic-account", "friend", "b" * 32 + ":portrait-v1", "friend", "rules")
        with self.store.connect() as connection:
            for selected in (one, two, unrelated):
                self.ledger.begin(connection, selected)
            clear_ledger(connection, account="synthetic-account", user="friend", source_id=raw)
        self.assertFalse(self.ledger.metadata(one)["initialized"])
        self.assertFalse(self.ledger.metadata(two)["initialized"])
        self.assertTrue(self.ledger.metadata(unrelated)["initialized"])

    def test_generic_raw_source_wildcards_are_escaped_and_scoped_source_is_exact(self):
        from api_portrait_ledger import clear_ledger
        raw = "source_%"
        exact = self.ledger.scope("synthetic-account", "friend", raw + ":portrait-v1", "friend", "rules")
        other_version = self.ledger.scope("synthetic-account", "friend", raw + ":portrait-v2", "friend", "rules")
        unrelated = self.ledger.scope("synthetic-account", "friend", "sourceXother:portrait-v1", "friend", "rules")
        with self.store.connect() as connection:
            for selected in (exact, other_version, unrelated):
                self.ledger.begin(connection, selected)
            clear_ledger(connection, account="synthetic-account", source_id=exact.source_id)
        self.assertFalse(self.ledger.metadata(exact)["initialized"])
        self.assertTrue(self.ledger.metadata(other_version)["initialized"])
        with self.store.connect() as connection:
            clear_ledger(connection, account="synthetic-account", source_id=raw)
        self.assertFalse(self.ledger.metadata(other_version)["initialized"])
        self.assertTrue(self.ledger.metadata(unrelated)["initialized"])

    def test_ledger_rows_and_fragment_metadata_do_not_store_raw_chat(self):
        raw = "SYNTHETIC-SENTINEL-LONG-RAW-CONVERSATION-ORIGINAL"
        self.commit([piece(1, raw)], signal())
        with self.store.connect() as connection:
            serialized = "".join(str(row) for row in connection.execute("SELECT * FROM api_portrait_ledger_rows_v1"))
            serialized += "".join(str(row) for row in connection.execute("SELECT * FROM api_portrait_ledger_fragments_v1"))
        self.assertNotIn(raw, serialized)
        self.assertNotIn('"text":', serialized)
        self.assertIn('"signal":', serialized)


if __name__ == "__main__":
    unittest.main()

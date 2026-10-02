"""Pinned-source-shaped synthetic fixtures for all three T04 input formats."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from qq_normalize import (ExportFormatError, chunk_paths, detect_export_format,
                          normalize_export, normalize_messages, read_export)

GOLDEN = json.loads((Path(__file__).parent / "fixtures/qq/qce6-golden.json").read_text(encoding="utf-8"))


class RawGoldenTests(unittest.TestCase):
    def test_raw_body_quote_large_ids_and_direction_match_fixed_expectations(self):
        result = normalize_messages([GOLDEN["raw"]], self_uin="10001")
        [record] = result["records"]
        expected = GOLDEN["expected"]
        self.assertEqual((record["native_id"], record["text"], record["quote"], record["time_ms"]),
                         (expected["nativeId"], expected["text"], expected["quote"], expected["rawTimeMs"]))
        self.assertEqual(record["native_seq"], "9223372036854775808")
        self.assertEqual(record["direction"], "peer")
        self.assertEqual(result["counts"]["rowsOk"], 1)

    def test_real_elements_take_precedence_over_flat_preview_text(self):
        row = {**GOLDEN["raw"], "text": "[引用预览] 不得进入正文"}
        result = normalize_messages([row], self_uin="10001")
        self.assertEqual(result["records"][0]["text"], GOLDEN["expected"]["text"])

    def test_missing_reply_record_does_not_invent_quote_or_drop_own_text(self):
        row = {**GOLDEN["raw"], "records": []}
        record = normalize_messages([row], self_uin="10001")["records"][0]
        self.assertIsNone(record["quote"])
        self.assertEqual(record["text"], GOLDEN["expected"]["text"])

    def test_media_only_raw_row_is_a_counted_placeholder(self):
        row = {**GOLDEN["raw"], "elements": [{"picElement": {"fileName": "synthetic.png"}}]}
        result = normalize_messages([row], self_uin="10001")
        self.assertEqual(result["records"][0]["kind"], "unknown")
        self.assertIsNone(result["records"][0]["text"])
        self.assertEqual(result["counts"]["unknownTypes"], 1)

    def test_negative_recall_is_counted_as_conflict(self):
        result = normalize_messages([{**GOLDEN["raw"], "recallTime": "-1"}], self_uin="10001")
        self.assertEqual((result["counts"]["rowsOk"], result["counts"]["rowsConflict"]), (0, 1))

    def test_canonical_uin_comparison_keeps_raw_uin(self):
        row = {**GOLDEN["raw"], "senderUin": "0010001", "sendType": "02"}
        record = normalize_messages([row], self_uin="10001")["records"][0]
        self.assertEqual((record["sender_uin"], record["direction"]), ("0010001", "self"))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.document = copy.deepcopy(GOLDEN["export"])

    def manifest(self):
        manifest = copy.deepcopy(self.document)
        del manifest["messages"]
        manifest["chunked"] = {"format": "jsonl", "chunksDir": "chunks",
                               "chunks": [{"relativePath": "chunks/0001.jsonl"},
                                          {"fileName": "0002.jsonl"}]}
        return manifest

    def test_single_export_preserves_subseconds_body_and_quote_without_owner_guessing(self):
        result = normalize_export(self.document)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["ownerUin"], "10001")
        first, second = result["records"]
        self.assertEqual(first["time_ms"], GOLDEN["expected"]["exportTimeMs"])
        self.assertEqual(second["time_ms"] - first["time_ms"], 877)
        self.assertEqual(first["text"], GOLDEN["expected"]["text"])
        self.assertEqual(first["quote"], GOLDEN["expected"]["quote"])
        self.assertEqual([first["direction"], second["direction"]], ["peer", "self"])

    def test_undeclared_owner_waits_and_explicit_confirmation_can_supply_it(self):
        del self.document["chatInfo"]["selfUin"]
        self.assertEqual(normalize_export(self.document)["status"], "pending-identity")
        self.assertEqual(normalize_export(self.document, self_uin="10001")["status"], "complete")

    def test_owner_mismatch_is_refused(self):
        with self.assertRaisesRegex(ExportFormatError, "owner-mismatch"):
            normalize_export(self.document, self_uin="10003")

    def test_group_metadata_and_third_sender_cannot_be_disguised_as_private(self):
        self.document["chatInfo"]["type"] = "group"
        with self.assertRaisesRegex(ExportFormatError, "not-single-chat"):
            normalize_export(self.document)
        self.document["chatInfo"]["type"] = "private"
        third = copy.deepcopy(self.document["messages"][0])
        third["sender"] = {"uin": "10003", "uid": "u_third"}
        self.document["messages"].append(third)
        with self.assertRaisesRegex(ExportFormatError, "not-single-chat"):
            normalize_export(self.document)

    def test_conflicting_peer_metadata_is_refused(self):
        self.document["chatInfo"]["peerUid"] = "u_wrong"
        with self.assertRaisesRegex(ExportFormatError, "peer-identity-mismatch"):
            normalize_export(self.document)

    def test_chunked_output_matches_single_export_and_conserves_bad_row_counts(self):
        one, two = self.document["messages"]
        chunks = {"chunks/0001.jsonl": json.dumps(one), "chunks/0002.jsonl": json.dumps(two)}
        single = normalize_export(self.document)
        chunked = normalize_export(self.manifest(), chunks=chunks)
        self.assertEqual(single["records"], chunked["records"])
        chunks["chunks/0002.jsonl"] += "\nprivate text that is not JSON\n"
        result = normalize_export(self.manifest(), chunks=chunks)
        self.assertEqual(result["status"], "partial")
        counts = result["counts"]
        self.assertEqual((counts["rowsTotal"], counts["rowsOk"], counts["rowsRejected"]), (3, 2, 1))
        self.assertNotIn("private", str(result["rejected"]))

    def test_chunk_path_traversal_and_duplicate_chunks_are_refused(self):
        for name in ("../outside.jsonl", "/abs.jsonl", "C:/file.jsonl", "..\\file.jsonl"):
            manifest = self.manifest()
            manifest["chunked"]["chunks"][0]["relativePath"] = name
            with self.subTest(name=name), self.assertRaises(ExportFormatError):
                chunk_paths(manifest)
        manifest = self.manifest()
        manifest["chunked"]["chunks"] *= 2
        with self.assertRaises(ExportFormatError):
            chunk_paths(manifest)

    def test_missing_chunk_and_row_budget_refuse_partial_complete_claims(self):
        with self.assertRaisesRegex(ExportFormatError, "missing-chunk"):
            normalize_export(self.manifest(), chunks={})
        with self.assertRaisesRegex(ExportFormatError, "row-budget"):
            normalize_export(self.document, max_rows=1)

    def test_version_and_ambiguous_timestamp_are_not_guessed(self):
        self.document["metadata"]["version"] = "99.0.0"
        with self.assertRaisesRegex(ExportFormatError, "unsupported-export-version"):
            normalize_export(self.document)
        self.document["metadata"]["version"] = "6.0.3"
        self.document["messages"][0]["timestamp"] = "2026-07-10T12:00:00"
        result = normalize_export(self.document)
        self.assertEqual(result["rejected"][0]["reason"], "invalid-time")

    def test_export_recall_boolean_has_no_fabricated_timestamp(self):
        self.document["messages"][0]["recalled"] = True
        record = normalize_export(self.document)["records"][0]
        self.assertEqual(record["status"], "recalled")
        self.assertIsNone(record["recall_time"])

    def test_file_entries_read_single_and_chunked_with_byte_budget(self):
        with TemporaryDirectory(prefix="qq-export-") as directory:
            root = Path(directory)
            path = root / "manifest.json"
            path.write_text(json.dumps(self.document), encoding="utf-8")
            self.assertEqual(detect_export_format(self.document), "qce-single-json")
            self.assertEqual(read_export(path)["status"], "complete")
            with self.assertRaisesRegex(ExportFormatError, "byte-budget"):
                read_export(path, max_bytes=5)
            path.write_text(json.dumps(self.manifest()), encoding="utf-8")
            (root / "chunks").mkdir()
            for index, row in enumerate(self.document["messages"], 1):
                (root / "chunks" / f"{index:04d}.jsonl").write_text(json.dumps(row), encoding="utf-8")
            self.assertEqual(read_export(path)["status"], "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)

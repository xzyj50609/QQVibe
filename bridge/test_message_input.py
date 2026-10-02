"""Synthetic tests for the unified message-input contract and legacy projection.

These tests never touch a real model, database, account or network. They pin the
compatibility rules that A5 must keep: legacy wire stays valid without metadata,
metadata never changes the model fields, and scope/identity are validated instead
of guessed.
"""
from __future__ import annotations

import json
import math
import unittest

import message_input
from node_analysis import NodeAnalysis


def source_item(**overrides):
    item = {"id": "m1", "side": "other", "kind": "text", "text": "你好\n第二行",
            "senderId": "member-a", "senderName": "阿甲", "time": 1700000000000,
            "_sort": [1, "shard", 1]}
    item.update(overrides)
    return item


class IdentityTests(unittest.TestCase):
    def test_self_message_keeps_the_source_sender_identity(self):
        record = message_input.build_input_record(
            source_item(side="self", senderId="me", senderName="我自己"))
        self.assertEqual((record["senderId"], record["senderName"]), ("me", "我自己"))

    def test_missing_sender_fields_stay_null_without_guessing(self):
        record = message_input.build_input_record(
            source_item(senderId=None, senderName=None))
        self.assertIsNone(record["senderId"])
        self.assertIsNone(record["senderName"])

    def test_empty_group_sender_is_unknown_not_a_real_member(self):
        record = message_input.build_input_record(source_item(senderId="", senderName=""))
        self.assertIsNone(record["senderId"])
        self.assertIsNone(record["senderName"])

    def test_two_other_members_keep_distinct_identity_through_json(self):
        prepared = message_input.prepare_messages(
            [source_item(id="a", senderId="member-a", senderName="阿甲"),
             source_item(id="b", senderId="member-b", senderName="阿乙")],
            account_id="acct", conversation_id="room@chatroom", source_kind="wechat")
        wire = [NodeAnalysis._wire_messages([item])[0] for item in prepared]
        round_tripped = json.loads(json.dumps(wire, ensure_ascii=False))
        meta = [message_input.normalize_input_meta(entry["inputMeta"]) for entry in round_tripped]
        self.assertEqual([entry["senderId"] for entry in meta], ["member-a", "member-b"])
        self.assertEqual([entry["senderName"] for entry in meta], ["阿甲", "阿乙"])
        self.assertEqual([entry["accountId"] for entry in meta], ["acct", "acct"])
        self.assertEqual([entry["conversationId"] for entry in meta],
                         ["room@chatroom", "room@chatroom"])


class QuoteTests(unittest.TestCase):
    def test_quote_text_keeps_newlines_tabs_and_emoji_verbatim(self):
        text = "第一行\n\t第二行 😀🙂"
        record = message_input.build_input_record(
            source_item(quote={"id": "q1", "senderId": "member-b",
                               "senderName": "阿乙", "text": text, "sentAtMs": 1700000000001}))
        self.assertEqual(record["quote"]["text"], text)

    def test_quote_accepts_draft_aliases_without_inventing_sender_id(self):
        record = message_input.build_input_record(
            source_item(quote={"id": "q1", "author": "阿乙", "text": "旧话", "time": 5}))
        self.assertEqual(record["quote"]["senderName"], "阿乙")
        self.assertEqual(record["quote"]["sentAtMs"], 5)
        self.assertIsNone(record["quote"]["senderId"])

    def test_missing_quote_stays_none_and_unknown_keys_are_dropped(self):
        self.assertIsNone(message_input.build_input_record(source_item())["quote"])
        self.assertIsNone(message_input.normalize_quote({"unexpected": "x"}))
        self.assertIsNone(message_input.normalize_quote({"text": "", "id": None}))

    def test_names_allow_display_control_but_ids_reject_control(self):
        quoted = message_input.normalize_quote({"senderName": "阿甲\n小号", "text": "换行"})
        self.assertEqual(quoted["senderName"], "阿甲\n小号")
        with self.assertRaises(ValueError):
            message_input.normalize_quote({"senderId": "bad\u0000id"})

    def test_main_text_is_never_truncated_or_joined_with_quote(self):
        record = message_input.build_input_record(
            source_item(text="新正文", quote={"text": "旧正文"}))
        self.assertEqual(record["text"], "新正文")


class TimeTests(unittest.TestCase):
    def test_float_time_is_preserved_without_unit_conversion(self):
        record = message_input.build_input_record(source_item(time=1.5))
        self.assertEqual(record["sentAtMs"], 1.5)
        self.assertIsInstance(record["sentAtMs"], float)
        wire = message_input.to_wire(source_item(time=1.5), record)
        self.assertEqual(wire["time"], 1.5)

    def test_zero_is_kept_as_the_unknown_sentinel(self):
        record = message_input.build_input_record(source_item(time=0))
        self.assertEqual(record["sentAtMs"], 0)

    def test_invalid_times_are_rejected_without_new_unit_guessing(self):
        for bad in (True, False, -1, -1.5, math.nan, math.inf, -math.inf, "1700000000000"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    message_input.build_input_record(source_item(time=bad))
        with self.assertRaises(ValueError):
            message_input.normalize_input_meta({"sentAtMs": True})

    def test_missing_time_is_null_not_the_current_clock(self):
        item = source_item()
        item.pop("time")
        self.assertIsNone(message_input.build_input_record(item)["sentAtMs"])


class MetadataMergeTests(unittest.TestCase):
    def test_existing_metadata_scope_is_filled_from_trusted_values(self):
        item = source_item(inputMeta={"quote": {"text": "旧引用"}})
        record = message_input.build_input_record(item, account_id="acct", conversation_id="friend")
        self.assertEqual(record["accountId"], "acct")
        self.assertEqual(record["conversationId"], "friend")
        self.assertEqual(record["quote"]["text"], "旧引用")

    def test_foreign_scope_is_rejected_not_overwritten(self):
        item = source_item(inputMeta={"accountId": "other-acct", "conversationId": "friend"})
        with self.assertRaises(ValueError):
            message_input.build_input_record(item, account_id="acct", conversation_id="friend")
        cross_chat = source_item(inputMeta={"conversationId": "other-chat"})
        with self.assertRaises(ValueError):
            message_input.build_input_record(cross_chat, account_id="acct",
                                            conversation_id="friend")

    def test_existing_metadata_is_preserved_when_trusted_scope_is_absent(self):
        item = source_item(time=7, inputMeta={"accountId": "acct", "senderId": "member-a",
                                      "sentAtMs": 7, "source": {"kind": "ocr"},
                                      "quote": {"text": "旧引用"}})
        record = message_input.build_input_record(item, source_kind=None)
        self.assertEqual(record["accountId"], "acct")
        self.assertEqual(record["source"]["kind"], "ocr")
        self.assertEqual(record["quote"]["text"], "旧引用")
        self.assertEqual(record["sentAtMs"], 7)

    def test_source_kind_defaults_to_unknown_and_backend_can_pin_wechat(self):
        self.assertEqual(message_input.build_input_record(source_item())["source"]["kind"],
                         "unknown")
        self.assertEqual(message_input.build_input_record(
            source_item(), source_kind="wechat")["source"]["kind"], "wechat")
        self.assertEqual(message_input.build_input_record(
            source_item(), source_kind="ocr")["source"]["kind"], "ocr")
        with self.assertRaises(ValueError):
            message_input.build_input_record(source_item(), source_kind="api:model")

    def test_qq_source_kind_is_accepted_and_keeps_its_metadata(self):
        """X10/C09: a QQ record passes the shared validation with its inputMeta intact."""
        item = source_item(senderId="123456789",
                           inputMeta={"accountId": "a:" + "0" * 32, "conversationId": "u:peer",
                                      "senderId": "123456789", "sentAtMs": 1_700_000_000_000,
                                      "source": {"kind": "qq"}, "quote": None})
        record = message_input.build_input_record(item, source_kind="qq")
        self.assertEqual(record["source"]["kind"], "qq")
        self.assertEqual(record["accountId"], "a:" + "0" * 32)
        self.assertEqual(message_input.SOURCE_KINDS, frozenset({"wechat", "qq", "ocr", "unknown"}))

    def test_existing_invalid_metadata_is_rejected(self):
        for bad in ("nope", {"source": "wechat"}, {"source": {"kind": "camera"}},
                    {"accountId": 7}, {"sentAtMs": math.inf},
                    {"senderName": "阿" * 201}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    message_input.build_input_record(source_item(inputMeta=bad))


class WireCompatibilityTests(unittest.TestCase):
    def test_legacy_id_allows_any_non_empty_string(self):
        long_id = "i" * 500
        record = message_input.build_input_record(source_item(id=long_id))
        self.assertEqual(record["id"], long_id)
        with self.assertRaises(ValueError):
            message_input.build_input_record(source_item(id=""))

    def test_prepare_item_preserves_legacy_keys_byte_for_byte(self):
        item = source_item()
        prepared = message_input.prepare_item(item, account_id="acct",
                                              conversation_id="friend", source_kind="wechat")
        for key in ("id", "side", "text", "kind", "time"):
            self.assertEqual(prepared[key], item[key])
        self.assertNotIn("inputMeta", item)
        self.assertEqual(prepared["inputMeta"]["accountId"], "acct")
        self.assertEqual(prepared["inputMeta"]["source"]["kind"], "wechat")

    def test_node_projection_is_the_same_with_and_without_metadata(self):
        item = source_item()
        prepared = message_input.prepare_item(item, account_id="acct",
                                              conversation_id="friend", source_kind="wechat")
        legacy = NodeAnalysis._wire_messages([item])[0]
        with_meta = NodeAnalysis._wire_messages([prepared])[0]
        self.assertEqual({key: with_meta[key] for key in legacy}, legacy)
        self.assertNotIn("inputMeta", legacy)
        self.assertIn("inputMeta", with_meta)

    def test_metadata_length_is_counted_by_codepoint(self):
        accepted = message_input.normalize_input_meta({"senderName": "😀" * 200})
        self.assertEqual(len(accepted["senderName"]), 200)
        with self.assertRaises(ValueError):
            message_input.normalize_input_meta({"senderName": "😀" * 201})

    def test_record_to_meta_carries_no_legacy_text_fields(self):
        record = message_input.build_input_record(source_item(), account_id="acct",
                                                  conversation_id="friend", source_kind="wechat")
        meta = message_input.record_to_meta(record)
        self.assertEqual(set(meta), {"accountId", "conversationId", "senderId", "senderName",
                                     "sentAtMs", "source", "quote"})


class AdditionalContractTests(unittest.TestCase):
    def test_null_source_and_extreme_time_are_consistent(self):
        self.assertEqual(message_input.normalize_input_meta({"source": {"kind": None}})["source"]["kind"], "unknown")
        for bad in (10 ** 400, {"kind": []}):
            with self.assertRaises(ValueError):
                message_input.normalize_input_meta({"sentAtMs": bad} if isinstance(bad, int) else {"source": bad})

    def test_conflicting_known_metadata_is_rejected(self):
        for meta in ({"senderId": "another"}, {"sentAtMs": 7}):
            with self.assertRaises(ValueError):
                message_input.build_input_record(source_item(inputMeta=meta))
        with self.assertRaises(ValueError):
            message_input.build_input_record(source_item(inputMeta={"source": {"kind": "ocr"}}), source_kind="wechat")

    def test_repeated_preparation_preserves_metadata(self):
        first = message_input.prepare_item(source_item(quote={"text": "原文\n🙂"}), account_id="acct", conversation_id="friend", source_kind="wechat")
        self.assertEqual(first, message_input.prepare_item(first, account_id="acct", conversation_id="friend", source_kind="wechat"))


class InputQueueBoundaryTests(unittest.TestCase):
    # Reuse setup/helpers only, without re-running the imported test class.
    import test_api_insights as fixtures
    setUp = fixtures.ApiInsightTests.setUp
    activate = fixtures.ApiInsightTests.activate
    wait_done = fixtures.ApiInsightTests.wait_done

    def test_invalid_metadata_leaves_no_queued_job_and_retry_can_start(self):
        self.activate()
        self.source.rows[2]["inputMeta"] = {"accountId": "foreign-account"}
        with self.assertRaises(ValueError):
            self.backend.start_model_insights("account-a", "friend", 1, target_ids=["o2"])
        self.assertEqual(self.backend.api_jobs, {})
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertEqual(self.analyzer.calls, [])
        del self.source.rows[2]["inputMeta"]
        self.backend.start_model_insights("account-a", "friend", 1, target_ids=["o2"])
        self.assertEqual(self.wait_done()["job"]["status"], "done")
        self.assertEqual(len(self.analyzer.calls), 1)

    def test_bad_metadata_outside_selected_context_is_not_examined(self):
        self.activate()
        self.source.rows[0]["inputMeta"] = {"accountId": "foreign-account"}
        for index in range(3, 9):
            self.source.rows.append({"id": f"o{index}", "side": "other", "kind": "text", "text": "合成消息",
                                     "senderId": "friend", "_sort": [index + 1, "shard", index + 1]})
        self.backend.start_model_insights("account-a", "friend", 1, target_ids=["o8"])
        self.assertEqual(self.wait_done()["job"]["status"], "done")
        self.assertEqual(len(self.analyzer.calls), 1)


if __name__ == "__main__":
    unittest.main()

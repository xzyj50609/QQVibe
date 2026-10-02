"""T04 normalization checks: synthetic QCE-shaped rows in, canonical records out.

Every case drives ``qq_normalize`` from raw input to output, because pre-seeding a
status in the store would only prove the store keeps what it was told (09 section 4).
"""
from __future__ import annotations

import unittest

import qq_normalize as normalize

SELF = "10001"
PEER_UID = "u_peer_synth01"


def row(**overrides):
    base = {"msgId": "756241784712300001", "msgSeq": "9223372036854775808",
            "msgTime": "1700000000", "chatType": 1, "peerUid": PEER_UID,
            "senderUin": "179000000001", "senderUid": "u_peer_synth01",
            "sendType": "0", "msgType": 2, "text": "在吗", "recallTime": "0"}
    base.update(overrides)
    return base


def one(**overrides):
    result = normalize.normalize_messages([row(**overrides)], self_uin=SELF)
    return result["records"]


class IdentityAndScopeTests(unittest.TestCase):
    def test_single_chat_key_is_the_peer_uid_anchor(self):
        self.assertEqual(one()[0]["conversation_key"], "u:" + PEER_UID)

    def test_group_shaped_input_is_rejected_not_squeezed(self):
        """X7: three-party records must not be filtered into a two-person chat."""
        for chat_type in (2, "2"):
            with self.subTest(chat_type=chat_type):
                result = normalize.normalize_messages([row(chatType=chat_type)], self_uin=SELF)
                self.assertEqual(result["records"], [])
                self.assertEqual(result["rejected"][0]["reason"], "unsupported-chat-type")
                self.assertEqual(result["counts"]["groupRejected"], 1)

    def test_an_unreadable_chat_type_is_rejected_as_invalid_rather_than_assumed(self):
        for chat_type in ("group", None, "1x"):
            with self.subTest(chat_type=chat_type):
                result = normalize.normalize_messages([row(chatType=chat_type)], self_uin=SELF)
                self.assertEqual(result["rejected"][0]["reason"], "invalid-chat-type")

    def test_missing_peer_uid_waits_instead_of_becoming_a_uin_key(self):
        result = normalize.normalize_messages([row(peerUid=None)], self_uin=SELF)
        self.assertEqual(result["rejected"][0]["reason"], "pending-identity")
        self.assertEqual(result["counts"]["pendingIdentity"], 1)

    def test_two_contacts_with_the_same_nickname_stay_distinct(self):
        """X2: display names never identify anyone; the peerUid does."""
        records = normalize.normalize_messages(
            [row(peerUid="u_peer_synth01"), row(peerUid="u_peer_synth02", msgId="756241784712300002")],
            self_uin=SELF)["records"]
        self.assertEqual([record["conversation_key"] for record in records],
                         ["u:u_peer_synth01", "u:u_peer_synth02"])


class PrecisionTests(unittest.TestCase):
    def test_big_ids_stay_the_exact_strings_they_arrived_as(self):
        """X4/V03: 9007199254740993 and 2**63 must not be widened to numbers."""
        record = one(msgId="9007199254740993", msgSeq="9223372036854775808")[0]
        self.assertEqual((record["native_id"], record["native_seq"]),
                         ("9007199254740993", "9223372036854775808"))
        self.assertIsInstance(record["native_id"], str)

    def test_numeric_json_ids_are_stringified_verbatim(self):
        record = one(msgId=756241784712300001)[0]
        self.assertEqual(record["native_id"], "756241784712300001")

    def test_blank_or_control_id_is_rejected(self):
        for bad in ("", "   ", "a\u0000b", [], True):
            with self.subTest(bad=bad):
                result = normalize.normalize_messages([row(msgId=bad)], self_uin=SELF)
                self.assertEqual(result["records"], [])
                self.assertEqual(result["rejected"][0]["reason"], "invalid-native-id")


class TimeTests(unittest.TestCase):
    def test_seconds_become_milliseconds_exactly_once(self):
        self.assertEqual(one()[0]["time_ms"], 1_700_000_000_000)

    def test_a_millisecond_looking_value_is_not_multiplied_again(self):
        result = normalize.normalize_messages([row(msgTime="1700000000000")], self_uin=SELF)
        self.assertEqual(result["rejected"][0]["reason"], "time-unit-ambiguous")

    def test_non_decimal_time_is_counted(self):
        for bad in ("17:00", None, -1, "1e9"):
            with self.subTest(bad=bad):
                result = normalize.normalize_messages([row(msgTime=bad)], self_uin=SELF)
                self.assertEqual(result["rejected"][0]["reason"], "invalid-time")


class DirectionTests(unittest.TestCase):
    def test_sender_equality_decides_self(self):
        self.assertEqual(one(senderUin=SELF, sendType="2")[0]["direction"], "self")

    def test_other_sender_is_peer(self):
        self.assertEqual(one(senderUin="179000000001", sendType="0")[0]["direction"], "peer")

    def test_system_send_type_is_system_not_a_person(self):
        self.assertEqual(one(sendType="3")[0]["direction"], "system")

    def test_padding_in_send_type_does_not_break_the_cross_check(self):
        self.assertEqual(one(senderUin=SELF, sendType="02")[0]["direction"], "self")

    def test_contradictory_direction_is_conflict_and_never_guessed(self):
        """V05/C04: senderUin says peer, sendType says self."""
        record = one(senderUin="179000000001", sendType="2")[0]
        self.assertEqual((record["direction"], record["status"]), ("conflict", "conflict"))
        result = normalize.normalize_messages([row(senderUin=SELF, sendType="0")], self_uin=SELF)
        self.assertEqual(result["counts"]["directionConflicts"], 1)
        self.assertEqual(result["counts"]["rowsConflict"], 1)

    def test_send_type_is_still_kept_raw_for_auditing(self):
        self.assertEqual(one(sendType="02")[0]["send_type"], "02")


class RecallTests(unittest.TestCase):
    def test_only_a_strictly_positive_value_means_recalled(self):
        record = one(recallTime="1700000123")[0]
        self.assertEqual((record["status"], record["recall_time"]), ("recalled", "1700000123"))

    def test_zero_and_absent_are_not_recalls(self):
        for raw in ("0", 0, None):
            with self.subTest(raw=raw):
                self.assertEqual(one(recallTime=raw)[0]["status"], "normal")

    def test_missing_or_null_shaped_value_counts_as_unknown_not_as_a_recall(self):
        for raw in (None, "", "  ", "null", "null "):
            with self.subTest(raw=raw):
                result = normalize.normalize_messages([row(recallTime=raw)], self_uin=SELF)
                self.assertEqual(result["records"][0]["status"], "normal")
                self.assertEqual(result["counts"]["unknownRecall"], 1)

    def test_negative_recall_time_is_a_conflict_not_a_silently_kept_row(self):
        record = one(recallTime="-5")[0]
        self.assertEqual((record["status"], record["recall_time"]), ("conflict", "-5"))

    def test_recall_keeps_the_original_text_untouched(self):
        record = one(recallTime="1700000123")[0]
        self.assertEqual(record["text"], "在吗")


class KindTests(unittest.TestCase):
    def test_known_text_type_keeps_the_body(self):
        record = one()[0]
        self.assertEqual((record["kind"], record["msg_type"], record["text"]), ("text", 2, "在吗"))

    def test_unmapped_type_is_unknown_and_counted_without_dropping_the_row(self):
        """V08: no invented mapping; the row survives as a counted unknown."""
        for msg_type in (7, 0, "22", None, "text"):
            with self.subTest(msg_type=msg_type):
                result = normalize.normalize_messages([row(msgType=msg_type, text="表情")],
                                                      self_uin=SELF)
                self.assertEqual(len(result["records"]), 1)
                self.assertEqual(result["records"][0]["kind"], "unknown")
                self.assertEqual(result["counts"]["unknownTypes"], 1)

    def test_unknown_kind_never_carries_a_body_into_analysis(self):
        record = normalize.normalize_messages([row(msgType=7, text="图片说明")],
                                              self_uin=SELF)["records"][0]
        self.assertIsNone(record["text"])

    def test_flat_body_wins_and_elements_are_the_fallback_shape(self):
        record = one(content={"elements": [{"type": "text", "text": "不该出现"}]})[0]
        self.assertEqual(record["text"], "在吗")

    def test_text_extracted_from_content_elements(self):
        record = one(text=None, content={"elements": [{"type": "text", "text": "第一行"},
                                                      {"type": "face", "text": "[表情]"}]})[0]
        self.assertEqual(record["text"], "第一行")


class QuoteTests(unittest.TestCase):
    def test_quote_is_a_separate_column(self):
        record = one(quote="原文")[0]
        self.assertEqual(record["quote"], "原文")

    def test_nested_quote_text_is_taken_and_nothing_is_guessed_about_its_author(self):
        record = one(quote={"text": "被引用的话"})[0]
        self.assertEqual(record["quote"], "被引用的话")

    def test_quote_of_the_wrong_shape_is_rejected(self):
        result = normalize.normalize_messages([row(quote=7)], self_uin=SELF)
        self.assertEqual(result["rejected"][0]["reason"], "invalid-quote")


class BatchTests(unittest.TestCase):
    def test_repeated_identical_text_in_the_same_second_keeps_both_rows(self):
        """X1: same second, same words, different msgId -> two records, never merged."""
        records = normalize.normalize_messages(
            [row(msgId="1"), row(msgId="2", text="在吗")], self_uin=SELF)["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual({record["native_id"] for record in records}, {"1", "2"})
        self.assertEqual({record["time_ms"] for record in records}, {1_700_000_000_000})

    def test_counters_are_conserved_over_the_whole_batch(self):
        rows = [row(), row(chatType=2), row(msgTime="bad"), row(sendType="2"),
                row(msgType=9)]
        counts = normalize.normalize_messages(rows, self_uin=SELF)["counts"]
        self.assertEqual(counts["rowsTotal"], len(rows))
        self.assertEqual(counts["rowsTotal"],
                         counts["rowsOk"] + counts["rowsRejected"] + counts["rowsConflict"])

    def test_rejection_reasons_never_contain_the_message_body(self):
        """R32/V40: the body stays in the record, not in the error channel."""
        result = normalize.normalize_messages([row(msgId="", text="很私密的正文")], self_uin=SELF)
        self.assertNotIn("很私密的正文", str(result["rejected"]))

    def test_normalize_version_is_stamped_on_every_record(self):
        self.assertEqual({record["normalize_version"] for record in one()},
                         {normalize.NORMALIZE_VERSION})

    def test_raw_row_is_preserved_without_reordering_the_record_fields(self):
        record = one(text="带 emoji 😀")[0]
        self.assertIn("带 emoji 😀", record["raw"])
        self.assertEqual(record["native_id_kind"], "qce-msgId")

    def test_missing_self_identity_rejects_the_batch_rather_than_guessing_direction(self):
        result = normalize.normalize_messages([row()], self_uin=None)
        self.assertEqual(result["rejected"][0]["reason"], "invalid-self-identity")


if __name__ == "__main__":
    unittest.main(verbosity=2)

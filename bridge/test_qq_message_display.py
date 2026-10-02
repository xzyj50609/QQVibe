"""Actual normalization/library/display boundaries using temporary QQ rows only."""
import copy
import json
import unittest

from qq_normalize import normalize_messages, normalize_export
from qq_message_display import display_metadata, MAX_QUOTE_CHARS
from qq_history_browser import browse
from message_input import prepare_messages, build_input_record, to_wire
from node_analysis import NodeAnalysis
import test_qq_source as source_fixture
from test_qq_export import GOLDEN


class DisplayTests(source_fixture.Fixture):
    def insert_raw(self, elements, *, identifier="1", msg_type=2):
        row = copy.deepcopy(GOLDEN["raw"])
        row.update(msgId=identifier, peerUid=source_fixture.CONV[2:], elements=elements, msgType=msg_type)
        result = normalize_messages([row], self_uin=source_fixture.UIN)
        self.assertEqual(result["counts"]["rowsRejected"], 0, result)
        self.store.ingest(self.account, source_fixture.CONV, result["records"])
        return self.source.messages(source_fixture.CONV, 80)[-1]

    def test_each_media_family_is_named_without_fetching_or_promoting_to_text(self):
        for index, (field, expected) in enumerate((("picElement", "image"), ("pttElement", "audio"),
                ("fileElement", "file"), ("videoElement", "video"), ("faceElement", "face"))):
            message = self.insert_raw([{field: {"fileName": "PRIVATE_TEST_NAME", "url": "https://never-fetch.invalid"},
                                       "textElement": None}], identifier=str(index + 1))
            self.assertEqual(message["qqDisplay"]["parts"], [expected])
            self.assertEqual(message["kind"], "other")
            self.assertNotIn("PRIVATE_TEST_NAME", json.dumps(message))
        self.assertEqual(self.source.profile_metadata(source_fixture.CONV)["textCount"], 0)

    def test_quote_and_body_stay_separate_through_source_history_and_model_input(self):
        raw = copy.deepcopy(GOLDEN["raw"])
        raw["peerUid"] = source_fixture.CONV[2:]
        rows = normalize_messages([raw], self_uin=source_fixture.UIN)["records"]
        self.store.ingest(self.account, source_fixture.CONV, rows)
        [message] = self.source.messages(source_fixture.CONV, 80)
        self.assertEqual(message["text"], GOLDEN["expected"]["text"])
        self.assertEqual(message["qqDisplay"]["quoteText"], GOLDEN["expected"]["quote"])
        self.assertEqual(set(message["qqDisplay"]), source_fixture.documented_dto("message.qqDisplay"))
        wire = prepare_messages([message], account_id=self.account,
            conversation_id=source_fixture.CONV, source_kind="qq")
        self.assertEqual(wire[0]["text"], GOLDEN["expected"]["text"])
        projected = to_wire(wire[0], build_input_record(wire[0]))
        self.assertNotIn("qqDisplay", projected)
        self.assertNotIn(GOLDEN["expected"]["quote"], json.dumps(projected, ensure_ascii=False))
        self.assertEqual(NodeAnalysis._wire_messages(wire), [projected])
        view = browse(self.source, self.account, source_fixture.CONV, limit=80)
        self.assertEqual(view["messages"][0]["qqDisplay"], message["qqDisplay"])

    def test_missing_reference_shows_missing_quote_without_inventing_text(self):
        message = self.insert_raw([{"replyElement": {"sourceMsgIdInRecords": "missing"}},
                                  {"textElement": {"content": "我自己的回复"}}])
        self.assertTrue(message["qqDisplay"]["hasQuote"])
        self.assertIsNone(message["qqDisplay"]["quoteText"])
        self.assertEqual(message["text"], "我自己的回复")

    def test_recall_hides_original_body_quote_and_media_descriptors(self):
        record = source_fixture.record("1", text="PRIVATE_RECALLED_BODY")
        record.update(status="recalled", quote="PRIVATE_QUOTE", recall_time="1",
                      raw=json.dumps({"elements": [{"picElement": {}}]}))
        self.store.ingest(self.account, source_fixture.CONV, [record])
        [message] = self.source.messages(source_fixture.CONV, 80)
        self.assertEqual(message["text"], "[消息已撤回]")
        self.assertNotIn("qqDisplay", message)
        self.assertNotIn("PRIVATE", json.dumps(message))

    def test_quote_is_bounded_without_changing_persisted_text(self):
        record = source_fixture.record("1")
        record["quote"] = "😀" * (MAX_QUOTE_CHARS + 1)
        self.store.ingest(self.account, source_fixture.CONV, [record])
        [message] = self.source.messages(source_fixture.CONV, 80)
        self.assertEqual(len(message["qqDisplay"]["quoteText"]), MAX_QUOTE_CHARS)
        self.assertTrue(message["qqDisplay"]["quoteTruncated"])
        self.assertEqual(self.store.connection.execute("SELECT length(quote) FROM messages").fetchone()[0], MAX_QUOTE_CHARS + 1)

    def test_sticker_uses_explicit_subtype_and_unknown_numeric_type_stays_unknown(self):
        message = self.insert_raw([{"picElement": {"picSubType": 1}}])
        self.assertEqual(message["qqDisplay"]["parts"], ["face"])
        unknown = self.insert_raw([], identifier="2", msg_type=9999)
        self.assertEqual(unknown["qqDisplay"]["parts"], ["unknown"])
        self.assertEqual(unknown["type"], "9999")

    def test_invalid_raw_metadata_does_not_break_local_reading(self):
        for raw in ("not JSON", json.dumps({"content": {"elements": [{"type": []}]}})):
            record = source_fixture.record("1", kind="unknown", text=None)
            record["raw"] = raw
            self.assertEqual(display_metadata(record)["parts"], ["unknown"])

    def test_directory_preview_names_media_and_quote_only_rows(self):
        self.insert_raw([{"pttElement": {}}])
        self.assertEqual(self.source.sessions()["sessions"][0]["preview"], "[语音]")
        self.insert_raw([{"replyElement": {"sourceMsgIdInRecords": "missing"}}], identifier="2")
        self.assertEqual(self.source.sessions()["sessions"][0]["preview"], "[引用消息]")


class ExportBodyTests(unittest.TestCase):
    def document(self, elements):
        document = copy.deepcopy(GOLDEN["export"])
        document["messages"] = document["messages"][:1]
        document["messages"][0]["content"].update(elements=elements, text="[图片][回复消息]渲染预览")
        document["messages"][0]["content"].pop("reply", None)
        return document

    def test_media_and_reply_preview_never_become_authored_analysis_text(self):
        document = self.document([{"type": "image", "data": {}},
                                 {"type": "reply", "data": {"content": "引用的别人的话"}}])
        result = normalize_export(document)
        [record] = result["records"]
        self.assertEqual((record["kind"], record["text"]), ("unknown", None))
        self.assertEqual(record["quote"], "引用的别人的话")
        self.assertEqual(display_metadata(record)["parts"], ["image"])

    def test_caption_keeps_only_structured_authored_text_in_original_order(self):
        document = self.document([{"type": "text", "data": {"text": "前文\n"}},
                                 {"type": "image", "data": {"url": "https://never-fetch.invalid"}},
                                 {"type": "text", "data": {"text": "后文😀"}}])
        record = normalize_export(document)["records"][0]
        self.assertEqual(record["text"], "前文\n后文😀")
        self.assertEqual(record["kind"], "text")

    def test_malformed_structured_text_is_rejected_instead_of_using_rendered_preview(self):
        document = self.document([{"type": "text", "data": {"text": []}}])
        result = normalize_export(document)
        self.assertEqual((result["counts"]["rowsRejected"], len(result["records"])), (1, 0))


if __name__ == "__main__":
    unittest.main()

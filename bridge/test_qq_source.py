"""Offline QQSource checks: contract-shaped answers without QCE, tokens or WeChat.

The DTO key sets are compared against the fenced ```dto blocks in
``docs/contracts/source-contract.md``, so the skeleton cannot quietly invent fields.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import qq_source
from backend_contracts import (AccountChangedError, AccountUnavailableError,
                              MessagesUnavailableError)
from product_profile import load_product
from qq_identity import account_key
from qq_message_store import QQMessageStore, database_path, message_key

CONTRACT = Path(__file__).resolve().parents[1] / "docs/contracts/source-contract.md"
CONV = "u:u_peer_synth01"
UIN = "10001"
PEER_UIN = "179000000001"


def documented_dto(name):
    blocks = dict(re.findall(r"```dto ([\w.]+)\n(.*?)```", CONTRACT.read_text(encoding="utf-8"), re.S))
    return {part.strip() for part in blocks[name].split(",") if part.strip()}


def record(native_id, *, time_ms=1_700_000_000_000, text="在吗", direction="peer",
           kind="text", msg_type=2):
    return {"native_id_kind": "qce-msgId", "native_id": native_id, "native_seq": "1",
            "conversation_key": CONV, "sender_uin": PEER_UIN if direction == "peer" else UIN,
            "sender_uid": "u_peer_synth01", "send_type": "0" if direction == "peer" else "2",
            "direction": direction, "time_ms": time_ms, "msg_type": msg_type, "kind": kind,
            "text": text, "quote": None, "status": "normal", "recall_time": "0",
            "raw": json.dumps({"msgId": native_id}), "normalize_version": "qq-v1"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="qqvibe-source-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = account_key(UIN)
        store = QQMessageStore(database_path(self.account, self.root))
        self.addCleanup(store.close)
        self.store = store
        store.ensure_conversation(self.account, CONV, peer_uid="u_peer_synth01",
                                  display_name="阿乙")
        self.source = qq_source.QQSource(uin=UIN, store=store, profile=load_product("qq"))
        self.addCleanup(self.source.close)


class ReadinessTests(Fixture):
    def test_an_unconfigured_account_is_a_real_error_not_an_empty_list(self):
        empty = qq_source.QQSource(profile=load_product("qq"))
        with self.assertRaises(AccountUnavailableError):
            empty.identity()
        with self.assertRaises(AccountUnavailableError):
            empty.verified_identity()
        with self.assertRaises(AccountUnavailableError):
            empty.sessions()

    def test_a_configured_account_without_a_library_reports_messages_unavailable(self):
        offline = qq_source.QQSource(uin=UIN, profile=load_product("qq"))
        with self.assertRaises(MessagesUnavailableError):
            offline.require_messages_ready()

    def test_the_account_anchor_survives_a_uid_appearing_later(self):
        """C03-R3: binding the same UIN again must not open a different library."""
        before = self.source.verified_identity()[0]
        self.source.attach(UIN)
        self.assertEqual(self.source.verified_identity()[0], before)

    def test_identity_data_root_is_the_pure_hex_directory(self):
        _account, directory = self.source.verified_identity()
        self.assertEqual(directory, self.store.path.parent)
        self.assertTrue(directory.is_relative_to(self.root))
        self.assertEqual(directory.parts[-2:], ("accounts", self.account[2:]))
        self.assertEqual(directory.parts[-3], "QQVibeData")

    def test_messages_true_requires_the_library(self):
        offline = qq_source.QQSource(uin=UIN, profile=load_product("qq"))
        with self.assertRaises(MessagesUnavailableError):
            offline.verified_identity(messages=True)


class DtoShapeTests(Fixture):
    def test_sessions_top_level_keys_match_the_contract(self):
        self.assertEqual(set(self.source.sessions()), documented_dto("sessions.top"))

    def test_session_items_and_contact_match_the_contract_keys(self):
        self.store.ingest(self.account, CONV, [record("1")])
        item = self.source.sessions()["sessions"][0]
        self.assertEqual(set(item), documented_dto("sessions.item"))
        self.assertEqual(set(self.source.contact(CONV)), documented_dto("contact"))
        self.assertFalse(item["isGroup"], "QQ single chats are not decided by a suffix")

    def test_rendered_rows_carry_exactly_the_documented_public_keys(self):
        self.store.ingest(self.account, CONV, [record("1")])
        window = self.source.messages(CONV, 10)
        public = {key for key in window[0] if not key.startswith("_")}
        self.assertEqual(public, documented_dto("message.row"))
        self.assertIn("_sort", window[0], "the internal position key stays for the backend")

    def test_an_empty_conversation_is_a_legal_empty_state(self):
        self.store.ensure_conversation(self.account, "u:u_quiet", peer_uid="u_quiet")
        result = self.source.sessions()
        self.assertEqual(result["messagesReady"], True)
        self.assertEqual(len(self.source.messages("u:u_quiet", 10)), 0)
        self.assertEqual(self.source.messages("u:u_quiet", 10).has_more_before, False)
        self.assertEqual(self.source.history_highwater("u:u_quiet"), None)
        self.assertEqual(self.source.conversation_coverage("u:u_quiet")["covered"], False)


class DirectionFilteringTests(Fixture):
    def test_system_and_unresolved_rows_are_dropped_not_rendered(self):
        self.store.ingest(self.account, CONV, [record("1", text="可见"),
                                               record("2", direction="system", text="系统"),
                                               record("3", direction="conflict", text="?)")])
        window = self.source.messages(CONV, 10)
        self.assertEqual([row["side"] for row in window], ["other"])
        self.assertEqual([row["id"] for row in window], ["1"])

    def test_unknown_kind_shows_a_placeholder_instead_of_a_blank_bubble(self):
        self.store.ingest(self.account, CONV, [record("1", kind="unknown", text=None, msg_type=None)])
        [row] = self.source.messages(CONV, 10)
        self.assertEqual((row["kind"], row["text"]), ("other", qq_source.UNKNOWN_KIND_LABEL))


class CursorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store.ingest(self.account, CONV, [record(str(i), time_ms=1_700_000_000_000 + i * 1000)
                                               for i in range(1, 6)])

    def test_a_short_final_page_still_returns_a_cursor(self):
        highwater = self.source.history_highwater(CONV)
        rows, cursor = self.source.history_page(CONV, highwater, None, page_size=2)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(cursor)
        self.assertIsInstance(cursor, tuple)
        tail, next_cursor = self.source.history_page(CONV, highwater, cursor, page_size=4)
        self.assertEqual(len(tail), 3)
        self.assertEqual(self.source.history_page(CONV, highwater, next_cursor, 4), ([], None))

    def test_a_cursor_from_another_conversation_is_refused(self):
        foreign = qq_source.encode_cursor(self.account, "u:someone_else", (1, "qq:u:someone_else", 1))
        with self.assertRaises(ValueError):
            qq_source.decode_cursor(foreign, self.account, CONV)

    def test_a_cursor_from_the_wechat_product_is_refused(self):
        import base64
        payload = json.dumps([1, "wechat", self.account, CONV, 1, 1]).encode()
        forged = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        with self.assertRaises(ValueError):
            qq_source.decode_cursor(forged, self.account, CONV)

    def test_a_missing_highwater_yields_no_page(self):
        self.assertEqual(self.source.history_page(CONV, None, None), ([], None))

    def test_preceding_text_context_only_exposes_id_side_text(self):
        rows, _cursor = self.source.history_page(CONV, self.source.history_highwater(CONV), None, page_size=5)
        context = self.source.preceding_text_context(CONV, rows[-1]["_sort"], limit=2)
        self.assertTrue(context)
        for entry in context:
            self.assertEqual(set(entry), {"id", "side", "text"})


class BatchAndAccountTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store.ingest(self.account, CONV, [record("1")])

    def test_batch_read_reports_per_conversation_metadata_not_a_bare_list(self):
        windows = self.source.message_windows([CONV, "u:none"], limit=10)
        self.assertEqual(sorted(windows), sorted([CONV, "u:none"]))
        self.assertEqual(set(windows.has_more_before), set(windows))
        self.assertIsInstance(windows.has_more_before, dict)

    def test_a_stale_expected_account_stops_the_whole_batch(self):
        with self.assertRaises(AccountChangedError):
            self.source.message_windows([CONV], expected_account="a:" + "f" * 32)

    def test_request_scope_detects_an_account_change_mid_request(self):
        with self.source.request_scope():
            pass          # nothing changed: the request ends quietly
        with self.assertRaises(AccountChangedError):
            with self.source.request_scope():
                self.source.attach("10002")

    def test_forgetting_an_unknown_account_is_a_no_op(self):
        before = self.source.verified_identity()[0]
        self.source.forget_account("a:" + "f" * 32)
        self.assertEqual(self.source.verified_identity()[0], before)

    def test_group_member_queries_are_refused_rather_than_answered_with_a_fake_member(self):
        with self.assertRaises(ValueError):
            self.source.stats(CONV, member=PEER_UIN)
        self.assertEqual(self.source.profile_metadata(CONV)["members"], [])
        self.assertEqual(self.source.profile_overview(CONV)["textCount"], None)
        self.assertEqual(self.source.profile_metadata(CONV)["count"], 1)


class MediaTests(Fixture):
    def test_media_is_absent_with_a_reason_instead_of_a_wechat_probe(self):
        self.assertIsNone(self.source.media(CONV, "1"))
        # real_http reads ``media_reason.value``; the reason must not be a WeChat reason code.
        self.assertEqual(self.source.media_reason.value, qq_source.MEDIA_REASON)

    def test_the_public_invalidation_entry_is_callable_and_idempotent(self):
        self.assertIsNone(self.source.invalidate_profile_metadata())
        self.assertIsNone(self.source.invalidate_profile_metadata(CONV))


class HealthGateTests(Fixture):
    def test_an_empty_qq_account_is_a_ready_empty_state_not_an_error(self):
        """Source contract 1.1: the health gate asks a capability, not a WeChat type."""
        from unittest.mock import Mock

        import backend_service
        backend = backend_service.Backend(
            self.source, analyzer=Mock(model={"state": "ready"}),
            model_source_store=Mock(saved_selection=lambda: {"selectedMode": "local"}),
            selection_store=backend_service.ConversationSelectionStore(self.root / "selection"))
        health = backend.health()
        self.assertEqual(health["data"]["state"], "ready")
        self.assertEqual(self.source.messages(CONV, 10).has_more_before, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

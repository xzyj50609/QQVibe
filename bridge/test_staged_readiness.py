"""Synthetic staged-readiness checks; no real WeChat process, keys, or chat data."""
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import live_source
from account_api import AccountAPI
from conversation_selection import ConversationSelectionStore
from live_source import ActiveSelection, LiveWeChatFactory, SessionOnlyWeChatDB
from real_backend import Backend, MessagesUnavailableError, WeChatSource
from real_http import make_handler


class ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class StagedReadinessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db_dir = self.root / "accounts"
        self.account = "synthetic_a"
        self.location = self.db_dir / self.account
        self.selection = ActiveSelection(self.location.resolve(), ((12345, 100.0),))
        self.files = {}
        for folder, name, marker in (("session", "session.db", 1),
                                     ("contact", "contact.db", 2),
                                     ("message", "message_0.db", 3)):
            self.add_database(folder, name, marker)
        self.keys = {rel: bytes([marker]) * 32 for rel, (_path, marker) in self.files.items()}
        stable = self.root / "stable"
        stable.mkdir()
        self.stable = stable / (self.account + ".json")
        env = patch.dict(os.environ, {"WECHATAUTO_KEYS_DIR": str(stable)})
        env.start()
        self.addCleanup(env.stop)
        verify = patch("live_source.upstream_db._verify_enc_key",
                       side_effect=lambda key, page: key[:1] == page[16:17])
        verify.start()
        self.addCleanup(verify.stop)
        active = patch("live_source.active_account_snapshot", side_effect=lambda: self.selection)
        active.start()
        self.addCleanup(active.stop)
        self.scan_keys = {}
        scanner = patch("live_source._scan_config_cipher_keys",
                        side_effect=lambda _pid, _pages, _metrics: dict(self.scan_keys))
        scanner.start()
        self.addCleanup(scanner.stop)
        self.log = io.StringIO()

    def add_database(self, folder, name, marker):
        path = self.location / "db_storage" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"S" * 16 + bytes([marker]) + b"P" * (4096 - 17))
        self.files[os.path.join(folder, name)] = (path, marker)
        return path

    def factory(self, retry_seconds=30):
        return LiveWeChatFactory(retry_seconds=retry_seconds)

    def call(self, factory):
        with patch("live_source.threading.Thread", ImmediateThread), redirect_stderr(self.log):
            return factory(db_dir=str(self.db_dir), account=self.account,
                           selection=self.selection)

    def scan_then_read(self, factory):
        with self.assertRaisesRegex(RuntimeError, "preparation pending"):
            self.call(factory)
        return self.call(factory)

    def source(self, factory):
        source = WeChatSource()
        source.factory = factory
        source.active_account_locator = lambda: self.selection
        return source

    def fake_anchor_open(self, _reader, rel):
        path = self.root / ("contact.sqlite" if rel.replace("\\", "/").rsplit("/", 1)[-1] == "contact.db"
                            else "session.sqlite")
        return sqlite3.connect(path)

    def make_anchor_sqlite(self):
        with closing(sqlite3.connect(self.root / "contact.sqlite")) as conn:
            conn.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
            conn.executemany("INSERT INTO contact VALUES (?,?,?)",
                             [(self.account, "Me", ""), ("peer", "Peer", "")])
            conn.commit()
        with closing(sqlite3.connect(self.root / "session.sqlite")) as conn:
            conn.execute("CREATE TABLE SessionTable(username TEXT, unread_count INTEGER, summary TEXT, "
                         "last_timestamp INTEGER, last_msg_sender TEXT, last_sender_display_name TEXT, "
                         "sort_timestamp INTEGER, is_hidden INTEGER)")
            conn.execute("INSERT INTO SessionTable VALUES (?,?,?,?,?,?,?,?)",
                         ("peer", 0, "hello", 100, "peer", "Peer", 100, 0))
            conn.commit()

    def test_exact_anchors_allow_sessions_http_200_but_block_message_and_analysis(self):
        session, contact, _message = self.files
        self.scan_keys = {rel: self.keys[rel] for rel in (session, contact)}
        factory = self.factory()
        reader = self.scan_then_read(factory)
        self.assertIsInstance(reader, SessionOnlyWeChatDB)
        self.assertFalse(reader.messages_ready)
        self.assertEqual({rel.replace("\\", "/") for rel, _path, _size in reader._db_files},
                         {"session/session.db", "contact/contact.db"})
        with self.assertRaisesRegex(RuntimeError, "messages unavailable"):
            reader._message_dbs()
        self.make_anchor_sqlite()
        source = self.source(factory)
        store_calls = []
        backend = Backend(source, analyzer=SimpleNamespace(model={"state": "ready"}),
                          store_factory=lambda *_args: store_calls.append(True),
                          selection_store=ConversationSelectionStore(self.root / "results"))
        self.addCleanup(backend.shutdown)
        accounts = AccountAPI(backend, self.root / "results", Path(reader.workdir).parent,
                              self.root / "stable")
        with patch.object(live_source.CacheOnlyWeChatDB, "_open",
                          lambda reader, rel: self.fake_anchor_open(reader, rel)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend, accounts))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, data = self.http_get(server, "/api/sessions")
                self.assertEqual(status, 200, data)
                self.assertFalse(data["messagesReady"])
                self.assertEqual(data["sessions"][0]["username"], "peer")
                status, selection = self.http_get(server, "/api/conversation-selection")
                self.assertEqual(status, 200, selection)
                self.assertEqual(selection, {"account": self.account, "initialized": False,
                                             "selectedSessions": []})
                for endpoint in ("/api/messages?user=peer", "/api/analysis?user=peer",
                                 "/api/profile?user=peer", "/api/history?account=synthetic_a&user=peer",
                                 "/api/history/search?account=synthetic_a&user=peer&q=hello"):
                    with self.subTest(endpoint=endpoint):
                        status, data = self.http_get(server, endpoint)
                        self.assertEqual(status, 503)
                        self.assertEqual(data["error"], "MessagesUnavailableError")
                for read in (lambda: source.messages("peer", 1),
                             lambda: source.history_highwater("peer"),
                             lambda: source.profile_metadata("peer"),
                             lambda: source.media("peer", "0" * 64)):
                    with self.assertRaises(MessagesUnavailableError):
                        read()
                self.assertEqual(store_calls, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    @staticmethod
    def http_get(server, path):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_missing_exact_anchor_never_yields_reader(self):
        session, contact, message = self.files
        self.scan_keys = {rel: self.keys[rel] for rel in (session, message)}
        factory = self.factory()
        with self.assertRaisesRegex(RuntimeError, "preparation pending"):
            self.call(factory)
        slot = next(iter(factory.slots.values()))
        self.assertEqual((slot.state, slot.keys), ("failed", None))
        with self.assertRaisesRegex(RuntimeError, "preparation pending"):
            self.call(factory)
        self.assertIn("contact=0/1", self.log.getvalue())

    def test_unrelated_database_missing_key_does_not_block_full_readiness(self):
        self.add_database("media", "media.db", 4)
        self.stable.write_text(json.dumps({rel: key.hex() for rel, key in self.keys.items()}),
                               encoding="utf-8")
        factory = self.factory()
        reader = self.call(factory)
        self.assertTrue(reader.messages_ready)
        self.assertEqual(factory.slots, {})

        self.stable.unlink()
        self.scan_keys = dict(self.keys)
        scanned = self.factory()
        self.assertTrue(self.scan_then_read(scanned).messages_ready)
        self.assertIn("other=0/0", self.log.getvalue())

    def test_fully_cached_anchors_and_messages_return_ready_without_scanning(self):
        self.stable.write_text(json.dumps({rel: key.hex() for rel, key in self.keys.items()}),
                               encoding="utf-8")
        factory = self.factory()
        reader = self.call(factory)
        self.assertTrue(reader.messages_ready)
        self.assertEqual(factory.slots, {})

    def test_partial_reader_upgrades_only_between_requests(self):
        session, contact, message = self.files
        self.scan_keys = {rel: self.keys[rel] for rel in (session, contact)}
        factory = self.factory(retry_seconds=0)
        self.scan_then_read(factory)
        source = self.source(factory)
        with source.request_scope():
            first = source._db(fresh=True)
            self.assertFalse(first.messages_ready)
            self.scan_keys = dict(self.keys)
            self.call(factory)  # A retry completes while this request holds its reader.
            self.assertIs(source._db(fresh=True), first)
            with self.assertRaises(MessagesUnavailableError):
                source.require_messages_ready()
        upgraded = source._db(fresh=True)
        self.assertTrue(upgraded.messages_ready)
        self.assertIsNot(upgraded, first)
        self.assertEqual(next(iter(factory.slots.values())).state, "ready")
        self.make_anchor_sqlite()
        with patch.object(live_source.CacheOnlyWeChatDB, "_open",
                          lambda reader, rel: self.fake_anchor_open(reader, rel)):
            self.assertTrue(source.sessions()["messagesReady"])

    def test_session_polling_stays_available_during_retry_then_upgrades(self):
        session, contact, _message = self.files
        self.scan_keys = {rel: self.keys[rel] for rel in (session, contact)}
        factory = self.factory(retry_seconds=0)
        self.scan_then_read(factory)
        source = self.source(factory)
        self.make_anchor_sqlite()
        with patch.object(live_source.CacheOnlyWeChatDB, "_open",
                          lambda reader, rel: self.fake_anchor_open(reader, rel)), \
                patch("live_source.threading.Thread", ImmediateThread), redirect_stderr(self.log):
            with source.request_scope():
                self.assertFalse(source.sessions()["messagesReady"])
            self.scan_keys = dict(self.keys)
            with source.request_scope():
                self.assertFalse(source.sessions()["messagesReady"])
            with source.request_scope():
                self.assertTrue(source.sessions()["messagesReady"])

    def test_account_and_page_change_invalidate_pinned_reader(self):
        session, contact, message = self.files
        self.scan_keys = dict(self.keys)
        factory = self.factory()
        self.scan_then_read(factory)
        source = self.source(factory)
        with source.request_scope():
            source._db(fresh=True)
            changed = bytearray(self.files[message][0].read_bytes())
            changed[16] = 9
            self.files[message][0].write_bytes(changed)
            with self.assertRaisesRegex(RuntimeError, "未就绪"):
                source.require_messages_ready()
        self.files[message][0].write_bytes(b"S" * 16 + b"\x03" + b"P" * (4096 - 17))
        with source.request_scope():
            source._db(fresh=True)
            other = self.db_dir / "synthetic_b"
            (other / "db_storage").mkdir(parents=True)
            self.selection = ActiveSelection(other.resolve(), ((99999, 200.0),))
            with self.assertRaisesRegex(RuntimeError, "未就绪"):
                source.require_messages_ready()

    def test_new_unkeyed_message_shard_blocks_saved_analysis_before_store_access(self):
        self.scan_keys = dict(self.keys)
        factory = self.factory()
        self.scan_then_read(factory)
        source = self.source(factory)
        source.require_messages_ready()
        store_calls = []
        backend = Backend(source, analyzer=SimpleNamespace(model={"state": "ready"}),
                          store_factory=lambda *_args: store_calls.append(True))
        self.addCleanup(backend.shutdown)
        self.add_database("message", "message_1.db", 4)
        with patch("live_source.threading.Thread", ImmediateThread), redirect_stderr(self.log):
            with self.assertRaisesRegex(RuntimeError, "未就绪"):
                backend.analysis("peer")
            with self.assertRaises(MessagesUnavailableError):
                backend.analysis("peer")
        self.assertEqual(store_calls, [])

    def test_zero_message_shards_does_not_complete_history_baseline(self):
        session, contact, message = self.files
        self.files[message][0].unlink()
        self.scan_keys = {rel: self.keys[rel] for rel in (session, contact)}
        factory = self.factory()
        reader = self.scan_then_read(factory)
        self.assertFalse(reader.messages_ready)
        self.assertEqual(next(iter(factory.slots.values())).state, "sessions")

    def test_reported_35_databases_24_keys_is_reproducible_without_private_data(self):
        # Mirrors the reported inventory, not its encrypted bytes or process memory.
        for index in range(1, 4):
            self.add_database("contact", f"contact_{index}.db", 3 + index)
        for index in range(1, 12):
            self.add_database("message", f"message_{index}.db", 6 + index)
        for index in range(18):
            self.add_database("media", f"media_{index}.db", 18 + index)
        self.keys = {rel: bytes([marker]) * 32 for rel, (_path, marker) in self.files.items()}
        self.assertEqual(len(self.files), 35)
        session = os.path.join("session", "session.db")
        contact = os.path.join("contact", "contact.db")
        extra_contact = os.path.join("contact", "contact_1.db")
        messages = [os.path.join("message", f"message_{index}.db") for index in range(11)]
        media = [os.path.join("media", f"media_{index}.db") for index in range(10)]
        known = [session, contact, extra_contact, *messages, *media]
        self.assertEqual(len(known), 24)
        self.scan_keys = {rel: self.keys[rel] for rel in known}
        factory = self.factory()
        reader = self.scan_then_read(factory)
        self.assertIsInstance(reader, SessionOnlyWeChatDB)
        self.assertFalse(reader.messages_ready)
        self.assertEqual(len(reader._db_files), 2)
        self.assertIn("required=14 matched=13 cache=0 scan_new=13 union=13", self.log.getvalue())
        self.assertIn("session=1/1 contact=1/1 message=11/12 other=0/0", self.log.getvalue())
        self.assertNotIn(self.account, self.log.getvalue())

    def test_unique_verified_contact_fallback_is_usable_in_selected_account(self):
        session, contact, message = self.files
        self.files[contact][0].unlink()
        self.add_database("misc", "contact.db", 2)
        fallback = os.path.join("misc", "contact.db")
        self.scan_keys = {rel: self.keys[rel] for rel in (session, message)}
        self.scan_keys[fallback] = b"\x02" * 32
        factory = self.factory()
        reader = self.scan_then_read(factory)
        self.assertTrue(reader.messages_ready)
        self.assertEqual(reader.anchor_rels["contact"], fallback)
        self.make_anchor_sqlite()
        source = self.source(factory)
        with patch.object(live_source.CacheOnlyWeChatDB, "_open",
                          lambda db, rel: self.fake_anchor_open(db, rel)):
            self.assertEqual(source.sessions()["sessions"][0]["username"], "peer")

    def test_ambiguous_verified_contact_fallbacks_are_rejected(self):
        session, contact, message = self.files
        self.files[contact][0].unlink()
        first = os.path.join("misc", "contact.db")
        second = os.path.join("misc2", "contact.db")
        self.add_database("misc", "contact.db", 2)
        self.add_database("misc2", "contact.db", 2)
        self.scan_keys = {rel: self.keys[rel] for rel in (session, message)}
        self.scan_keys[first] = self.scan_keys[second] = b"\x02" * 32
        factory = self.factory()
        with self.assertRaisesRegex(RuntimeError, "preparation pending"):
            self.call(factory)
        self.assertEqual(next(iter(factory.slots.values())).state, "failed")

    def test_owner_query_includes_fallback_even_when_canonical_file_exists(self):
        fallback = self.add_database("misc", "contact.db", 2)
        account = SimpleNamespace(path=str(self.location / "db_storage"))
        owned_files = live_source._account_database_files(account)
        self.assertIn(str(fallback.resolve()), owned_files)
        self.assertIn(str(self.files[os.path.join("contact", "contact.db")][0].resolve()),
                      owned_files)

    def test_verified_fallback_with_invalid_contact_schema_is_rejected(self):
        session, contact, message = self.files
        self.files[contact][0].unlink()
        fallback = os.path.join("misc", "contact.db")
        self.add_database("misc", "contact.db", 2)
        self.scan_keys = {rel: self.keys[rel] for rel in (session, message)}
        self.scan_keys[fallback] = b"\x02" * 32
        factory = self.factory()
        self.scan_then_read(factory)
        with closing(sqlite3.connect(self.root / "contact.sqlite")) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
        with closing(sqlite3.connect(self.root / "session.sqlite")) as conn:
            conn.execute("CREATE TABLE SessionTable(username TEXT)")
        source = self.source(factory)
        with patch.object(live_source.CacheOnlyWeChatDB, "_open",
                          lambda db, rel: self.fake_anchor_open(db, rel)):
            with self.assertRaises(sqlite3.DatabaseError):
                source.sessions()


if __name__ == "__main__":
    unittest.main()

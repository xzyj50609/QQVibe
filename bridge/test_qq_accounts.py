"""QQ lifecycle tests; no installed-client paths, QCE, credentials or user data."""
from __future__ import annotations

import json
import http.client
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from account_store import AccountConflict, AccountNotFound, account_id
from backend_contracts import AccountUnavailableError
from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from product_profile import load_product
from qq_account_api import QQAccountAPI
from qq_account_store import QQAccountStore
from qq_identity import account_key
from qq_source import QQSource
from real_http import build_account_api, make_handler
from result_store import ResultStore
from test_account_store import directory_reparse_alias
from test_qq_message_store import CONV, record


class QQAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qq-accounts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_root = self.root / "QQVibeData"
        self.source = QQSource(root=self.root, profile=load_product("qq"))
        def results(account, _workdir):
            directory = self.data_root / "real-client-data"
            directory.mkdir(parents=True, exist_ok=True)
            return ResultStore(directory / (account_id(account) + ".sqlite3"))
        self.backend = Backend(
            self.source, analyzer=SimpleNamespace(model={"state": "ready"}, analysis_version=lambda: "synthetic", close=lambda: None),
            model_source_store=Mock(saved_selection=lambda: {"selectedMode": "local"}),
            selection_store=ConversationSelectionStore(self.data_root / "real-client-data"),
            store_factory=results)
        self.addCleanup(self.backend.shutdown)
        self.api = QQAccountAPI(self.backend, self.data_root)
        self.store = self.api.store

    def bind(self, owner):
        account = self.source.attach(owner)
        library = self.source._store
        library.ensure_conversation(account, CONV, display_name="合成联系人")
        library.ingest(account, CONV, [record("1")])
        self.api.observe(self.source.sessions())
        return account, library.path

    def test_empty_qq_account_list_does_not_construct_or_discover_wechat(self):
        with patch("account_api.AccountStore", side_effect=AssertionError("WeChat store")), \
                patch("account_api.try_forget_account", side_effect=AssertionError("WeChat cache")):
            api = build_account_api(self.backend, load_product("qq"), self.root)
            self.assertIsInstance(api, QQAccountAPI)
            self.assertEqual(api.list(), {"accounts": [], "currentAccountId": None, "platform": "qq"})
        self.assertFalse(self.data_root.exists())

    def test_qq_health_stays_offline_and_branding_is_public_without_an_account(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend, self.api))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def get(path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                return response.getheader("Content-Type"), response.read().decode("utf-8")
            finally:
                connection.close()
        with patch("real_http.PRODUCT", load_product("qq")):
            health = json.loads(get("/api/health")[1])
            self.assertFalse(health["source"]["localReady"])
            self.assertFalse(health["source"]["sync"]["enabled"])
            self.assertEqual(health["source"]["connection"], "offline")
            mime, config = get("/product-config.js")
            self.assertIn("javascript", mime)
            self.assertIn('"key": "qq"', config)
            self.assertNotIn("Token", config)
            html = get("/")[1]
            self.assertIn("<title>QQVibe</title>", html)
            self.assertIn("已保存的 QQ 账号", html)
            self.assertIn("github.com/tswawa/WechatVibe", html, "upstream attribution stays intact")
            self.assertNotIn("微信账号", html)
            self.assertIn("qq-startup.js", html)
            self.assertFalse(self.data_root.exists(), "empty startup never creates an account library")
            self.bind("10001")
            health = json.loads(get("/api/health")[1])
            self.assertTrue(health["source"]["localReady"])
            self.assertEqual(health["source"]["sync"]["state"], "unavailable")
        with patch("real_http.PRODUCT", load_product("wechat")):
            self.assertIn('"key": "wechat"', get("/product-config.js")[1])
            self.assertIn("<title>WechatVibe</title>", get("/")[1])

    def test_saved_qq_account_reopens_after_bridge_restart(self):
        account, path = self.bind("10001")
        self.source.forget_account(account)
        self.assertFalse(self.api.source_status()["localReady"])
        self.assertTrue(path.is_file())
        self.api.activate(account_id(account))
        self.assertTrue(self.api.source_status()["localReady"])
        self.assertEqual(len(self.source.messages(CONV, 10)), 1)

    def test_colon_bearing_account_key_registers_and_can_select_a_conversation(self):
        account, path = self.bind("10001")
        selected = self.backend.set_conversation_selected(account, CONV, True)
        self.assertEqual(selected["selectedSessions"], [CONV])
        self.assertEqual(self.backend.conversation_selection()["selectedSessions"], [CONV])
        [item] = self.api.list()["accounts"]
        self.assertEqual((item["displayId"], item["platform"], item["current"]), ("10001", "qq", True))
        self.assertEqual(item["accountId"], account_id(account))
        self.assertGreater(item["bytes"], path.stat().st_size)

    def test_saved_offline_account_can_be_activated_without_qce_or_new_library_creation(self):
        first, first_path = self.bind("10001")
        second, _ = self.bind("10002")
        result = self.api.activate(account_id(first))
        self.assertEqual(result["account"], first)
        self.assertTrue(result["offline"])
        self.assertEqual(self.source.identity()[1], first_path.parent)
        self.assertEqual(self.api.list()["currentAccountId"], account_id(first))
        self.assertEqual(len(self.source.messages(CONV, 10)), 1)
        self.assertNotEqual(first, second)

    def test_http_activation_is_intentional_scope_change_and_returns_one_success_response(self):
        first, _ = self.bind("10001")
        self.bind("10002")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend, self.api))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST", "/api/accounts/activate", json.dumps({"accountId": account_id(first)}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["account"], first)
        finally:
            connection.close()

    def test_registry_rejects_an_owner_or_path_that_does_not_match_the_hashed_key(self):
        account, path = self.bind("10001")
        with self.assertRaises(AccountConflict):
            self.store.register(account, path.parent, "10002")
        with self.assertRaises(AccountConflict):
            self.store.register(account, self.root, "10001")
        document = json.loads(self.store.registry.read_text(encoding="utf-8"))
        document["accounts"][0]["workdir"] = str(self.root)
        self.store.registry.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.api.list()["accounts"], [])
        with self.assertRaises(AccountNotFound):
            self.api.delete(account_id(account))
        self.assertTrue(path.is_file())

    def test_inactive_deletion_removes_only_that_qq_library_and_its_result_selection_file(self):
        first, first_path = self.bind("10001")
        self.backend.set_conversation_selected(first, CONV, True)
        second, second_path = self.bind("10002")
        untouched = self.root / "wechat" / "source.db"
        untouched.parent.mkdir()
        untouched.write_bytes(b"synthetic outside data")
        config = self.data_root / "real-client-runtime" / "model-settings.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(b"synthetic global settings")
        with patch("account_api.try_forget_account", side_effect=AssertionError("WeChat callback")):
            result = self.api.delete(account_id(first))
        self.assertEqual(result, {"deleted": account_id(first), "current": False, "exitApp": False})
        self.assertFalse(first_path.parent.exists())
        self.assertFalse((self.store.data_dir / (account_id(first) + ".sqlite3")).exists())
        self.assertTrue(second_path.is_file())
        self.assertEqual(self.source.identity()[0], second)
        self.assertEqual(len(self.source.messages(CONV, 10)), 1)
        self.assertEqual(untouched.read_bytes(), b"synthetic outside data")
        self.assertEqual(config.read_bytes(), b"synthetic global settings")

    def test_current_deletion_drains_source_and_leaves_recovery_disabled(self):
        account, path = self.bind("10001")
        old = self.source._store
        manager = Mock()
        self.api.sync_manager = manager
        with patch("account_api.wait_forget_account", side_effect=AssertionError("WeChat callback")):
            result = self.api.delete(account_id(account))
        manager.stop_account.assert_called_once_with(account)
        self.assertTrue(result["exitApp"])
        self.assertTrue(old.closed)
        self.assertFalse(path.parent.exists())
        self.assertTrue(self.api.no_recovery_marker.is_file())
        self.assertEqual(self.api.list()["accounts"], [])

    def test_move_failure_rolls_back_and_restores_current_offline_binding(self):
        account, path = self.bind("10001")
        real_replace = os.replace
        def fail_message_move(source, target):
            if Path(source) == path.parent:
                raise PermissionError("synthetic file in use")
            return real_replace(source, target)
        with patch("qq_account_store.os.replace", side_effect=fail_message_move):
            with self.assertRaises(PermissionError):
                self.api.delete(account_id(account))
        self.assertFalse(self.store.is_deleting(account))
        self.assertEqual(self.source.identity()[0], account)
        self.assertEqual(len(self.source.messages(CONV, 10)), 1)
        self.assertFalse(self.api.no_recovery_marker.exists())

    def test_purge_failure_is_visible_and_retry_does_not_recreate_an_empty_library(self):
        account, path = self.bind("10001")
        with patch.object(self.store, "_purge", side_effect=PermissionError("synthetic")):
            with self.assertRaises(AccountConflict):
                self.api.delete(account_id(account))
        self.assertTrue(self.store.is_deleting(account))
        self.assertFalse(path.exists())
        self.assertTrue(self.api.list()["accounts"][0]["deletionPending"])
        with self.assertRaises(AccountUnavailableError):
            self.source.attach("10001")
        self.assertFalse(path.exists(), "failed clear must not silently open an empty replacement")
        result = self.api.delete(account_id(account))
        self.assertEqual(result["deleted"], account_id(account))
        self.assertEqual(self.api.list()["accounts"], [])
        self.assertEqual(list(self.store.deletion_root.iterdir()), [])

    def test_unfinished_staging_can_be_resumed_after_a_process_restart(self):
        account, path = self.bind("10001")
        self.source.forget_account(account)
        records = self.store._read_registry()
        identifier = account_id(account)
        records[identifier].update(deletionId=identifier + "-" + "1" * 32, deletionState="staging")
        self.store._write_registry(records)
        staging = self.store.deletion_root / records[identifier]["deletionId"]
        staging.mkdir(parents=True)
        os.replace(path.parent, staging / "messages")
        recovered = QQAccountStore(self.data_root)
        result = recovered.delete(identifier, guard=lambda _: None, forget=lambda _: True)
        self.assertEqual(result["deleted"], identifier)
        self.assertEqual(recovered.list()["accounts"], [])

    def test_reparse_point_inside_account_or_at_registry_parent_is_refused(self):
        account, path = self.bind("10001")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep")
        link = path.parent / "external"
        directory_reparse_alias(outside, link)
        with self.assertRaises(AccountConflict):
            self.store.delete(account_id(account), guard=lambda _: None, forget=lambda _: True)
        self.assertEqual((outside / "keep.txt").read_bytes(), b"keep")
        self.assertTrue(path.exists())
        link.rmdir() if getattr(os.path, "isjunction", lambda _: False)(link) else link.unlink()
        other_root = self.root / "other-product"
        other_root.mkdir()
        directory_reparse_alias(self.store.data_dir, other_root / "real-client-data")
        with self.assertRaises(AccountConflict):
            QQAccountStore(other_root).list()

    def test_attach_refuses_a_reparse_account_root_before_creating_data(self):
        self.data_root.mkdir()
        outside = self.root / "outside-account-data"
        outside.mkdir()
        directory_reparse_alias(outside, self.data_root / "accounts")
        with self.assertRaises(AccountConflict):
            self.source.attach("10001")
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(AccountUnavailableError):
            self.source.identity()


if __name__ == "__main__":
    unittest.main(verbosity=2)

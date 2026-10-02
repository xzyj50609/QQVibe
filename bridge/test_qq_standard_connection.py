"""Standard connection is explicit, bounded and never returns secrets to UI."""
import json
import tempfile
import unittest
import http.client
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from qq_connector import ConnectorError
from qq_standard_connection import standard_connection, STANDARD_BASE


class StandardConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "QQChatExporter/.qce-config/security.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"accessToken": "synthetic-only", "secretKey": "unused"}), encoding="utf-8-sig")
        self.connector = Mock()
        self.connector.identity.return_value = {"ownerUin": "10001", "version": "6.3.0"}
        self.factory = Mock(return_value=self.connector)

    def read(self):
        return standard_connection(local_app_data=self.root, connector_factory=self.factory)

    def test_reads_only_fixed_config_and_identity_no_contacts_or_history(self):
        self.assertEqual(self.read(), (STANDARD_BASE, "10001", "synthetic-only"))
        self.factory.assert_called_once_with(STANDARD_BASE, "synthetic-only")
        self.assertEqual([call[0] for call in self.connector.mock_calls], ["client", "identity"])

    def test_missing_expired_corrupt_and_oversize_are_distinct(self):
        self.path.unlink()
        with self.assertRaisesRegex(ConnectorError, "standard-installation-missing"):
            self.read()
        for raw in ('{}', 'bad json', '{"tokenExpired":true}', 'x' * 16385):
            self.path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ConnectorError):
                self.read()
        self.factory.assert_not_called()

    def test_errors_do_not_expose_token(self):
        self.connector.identity.side_effect = RuntimeError("synthetic-only")
        with self.assertRaisesRegex(ConnectorError, "connection-unavailable") as raised:
            self.read()
        self.assertNotIn("synthetic-only", str(raised.exception))

    def test_rejects_relative_root(self):
        with self.assertRaisesRegex(ConnectorError, "standard-installation-missing"):
            standard_connection(local_app_data="relative", connector_factory=self.factory)

    def test_account_change_does_not_replace_configuration(self):
        from qq_sync import QQSync
        from contextlib import nullcontext
        sync = Mock()
        sync.live_validated = True
        sync.accounts.lock = nullcontext()
        sync.source.lock = nullcontext()
        sync.source.identity.return_value = ("a:" + "0" * 32, Path("unused"))
        with patch("qq_standard_connection.standard_connection", return_value=(STANDARD_BASE, "10001", "synthetic-only")):
            with self.assertRaisesRegex(ConnectorError, "account-changed"):
                QQSync.connect_standard(sync)
        sync.configure.assert_not_called()
        sync.connect.assert_not_called()

    def test_http_action_rejects_extra_fields_and_never_returns_credential(self):
        from real_http import make_handler
        sync = Mock()
        sync.connect_standard.return_value = {"state": "online", "ownerUin": "10001", "tokenConfigured": True}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SimpleNamespace(), SimpleNamespace(sync_manager=sync)))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("POST", "/api/qq/connection/connect-standard", json.dumps(body),
                                   {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        self.assertEqual(request({"path": "untrusted"})[0], 400)
        sync.connect_standard.assert_not_called()
        status, public = request({})
        self.assertEqual(status, 200)
        self.assertEqual(public["tokenConfigured"], True)
        self.assertNotIn("token", public)
        sync.connect_standard.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

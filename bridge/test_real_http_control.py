"""Control endpoint checks using only a synthetic backend and loopback server."""

import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_http
from real_http import CONTROL_TOKEN_HEADER, ROOT, app_version, make_handler


class StubBackend:
    def health(self):
        return {"version": "real-ui-1", "ok": True}


class AssemblyTests(unittest.TestCase):
    def test_the_qq_product_never_builds_a_wechat_source(self):
        """T03-B: the QQ entry must not scan WeChat accounts or read its keys."""
        import wechat_source
        from product_profile import load_product
        with patch.object(wechat_source.WeChatSource, "__init__",
                          side_effect=AssertionError("WeChat assembly ran in QQ mode")):
            source = real_http.build_source(classifier=None, profile=load_product("qq"))
        self.assertEqual(source.kind, "qq")

    def test_the_wechat_product_still_builds_the_wechat_source(self):
        from product_profile import load_product
        source = real_http.build_source(classifier=None, profile=load_product("wechat"))
        self.assertEqual(source.kind, "wechat")

    def test_the_health_gate_asks_the_source_instead_of_testing_its_class(self):
        import backend_service
        with open(backend_service.__file__, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("isinstance(self.source, WeChatSource) or self.source.db", text)


class ControlTests(unittest.TestCase):
    def server(self, token):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(StubBackend(), control_token=token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, thread

    @staticmethod
    def request(server, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request(method, path, body=b"" if method == "POST" else None,
                               headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_control_requires_exact_token_and_trusted_loopback_request(self):
        token = "a" * 64
        server, thread = self.server(token)
        with patch.object(real_http, "app_version", side_effect=AssertionError("disk version reread")):
            status, health = self.request(server, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["appVersion"], app_version())
        self.assertEqual(health["appVersion"], json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"])
        self.assertNotIn("control_token", health)
        self.assertNotIn("controlToken", health)
        self.assertNotIn(token, json.dumps(health))
        self.assertEqual(self.request(server, "POST", "/api/control/shutdown")[0], 403)
        self.assertEqual(self.request(server, "POST", "/api/control/shutdown",
                                      {CONTROL_TOKEN_HEADER: "b" * 64})[0], 403)
        self.assertEqual(self.request(server, "POST", "/api/control/shutdown",
                                      {CONTROL_TOKEN_HEADER: token, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request(server, "POST", "/api/control/shutdown",
                                      {CONTROL_TOKEN_HEADER: token, "Host": "evil.example"})[0], 403)
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.request(server, "POST", "/api/control/shutdown",
                                      {CONTROL_TOKEN_HEADER: token}), (202, {"stopping": True}))
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_control_is_disabled_without_child_environment_token(self):
        server, thread = self.server(None)
        self.assertEqual(self.request(server, "POST", "/api/control/shutdown",
                                      {CONTROL_TOKEN_HEADER: "a" * 64})[0], 403)
        self.assertTrue(thread.is_alive())

    def test_javascript_static_resources_ignore_windows_registry_mime(self):
        token = "a" * 64
        server, thread = self.server(token)
        self.addCleanup(server.shutdown)
        self.assertEqual(real_http.static_content_type("app.js"), real_http.JAVASCRIPT_MIME)
        self.assertEqual(real_http.static_content_type("module.mjs"), real_http.JAVASCRIPT_MIME)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            with patch.object(real_http.mimetypes, "guess_type", return_value=("text/plain", None)):
                connection.request("GET", "/app.js")
                response = connection.getresponse()
                body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), real_http.JAVASCRIPT_MIME)
            self.assertIn(b"function", body)
        finally:
            connection.close()
            server.shutdown()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()

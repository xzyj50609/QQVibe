"""Model-source security and HTTP contract checks without provider or chat access."""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_model_source import ModelSource
import local_model_source
from model_bundle import ModelBundleError
from model_source import LOCAL_SOURCE_ID, ModelSourceStore, ModelSourceUnavailable, connection_values
from real_backend import Backend
from api_tasks import ApiTaskCoordinator
from real_http import make_handler


class FakeAnalyzer:
    def __init__(self):
        self.calls = []

    def model_list(self, protocol, base_url, api_key):
        self.calls.append(("list", protocol, base_url, api_key))
        return {"models": [{"id": "test-model", "name": "Test Model"}], "supported": True}

    def model_test(self, protocol, base_url, api_key, model):
        self.calls.append(("test", protocol, base_url, api_key, model))
        return {"ok": True, "latencyMs": 12.5}


class CancellableAnalyzer(FakeAnalyzer):
    def __init__(self):
        super().__init__()
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


class ModelSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        runtime = local_model_source.PRODUCT.state_dir("real-client-runtime", root)
        self.path = runtime / "api-model-source.json"
        self.legacy = runtime / "model-source.json"
        self.store = ModelSourceStore(
            self.path, root=root, legacy_path=self.legacy,
            protect=lambda key: b"protected:" + key[::-1].encode("utf-8"),
            unprotect=lambda data: data.removeprefix(b"protected:").decode("utf-8")[::-1],
        )
        self.local_source = ModelSource(root)
        self.backend = object.__new__(Backend)
        self.backend.analyzer = FakeAnalyzer()
        self.backend.api_analyzer = self.backend.analyzer
        self.backend.api_portrait_analyzer = self.backend.analyzer
        self.backend.api_probe_analyzer = self.backend.analyzer
        self.backend.api_tasks = ApiTaskCoordinator()
        self.backend.api_portrait_jobs = self.backend.api_tasks.portrait_jobs
        self.backend.api_lock = self.backend.api_tasks.lock
        self.backend.api_condition = self.backend.api_tasks.condition
        self.backend.api_jobs = self.backend.api_tasks.insight_jobs
        self.backend.model_source_revision = 0
        self.backend.closing = False
        self.backend.model_source_store = self.store
        self.backend.active_model_source_mode = "local"
        self.backend.active_model_source_id = LOCAL_SOURCE_ID
        self.backend.active_api_config = None
        self.backend.request_lease = nullcontext

    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_switching_to_local_cancels_old_api_workers_without_erasing_source_identity(self):
        insight = CancellableAnalyzer()
        portrait = CancellableAnalyzer()
        self.backend.api_analyzer = insight
        self.backend.api_portrait_analyzer = portrait
        source_id = self.store.save_api("responses", "https://example.test/v1", "test-model",
                                        "test-only-key", context_tokens=128000)
        self.backend.active_model_source_mode = "api"
        self.backend.active_model_source_id = source_id
        self.backend.active_api_config = {"protocol": "responses", "baseUrl": "https://example.test/v1",
                                          "model": "test-model", "contextTokens": 128000}
        self.backend.api_jobs[("account-a", "friend", source_id)] = {"status": "running"}
        self.backend.api_portrait_jobs[("account-a", "friend", source_id, "friend")] = {"status": "running"}
        self.backend.model_source_activate({"mode": "local"})
        self.assertEqual((insight.cancelled, portrait.cancelled), (1, 1))
        self.assertEqual(self.backend.api_jobs, {})
        self.assertEqual(self.backend.api_portrait_jobs, {})
        self.assertEqual(self.store.saved_selection()["sourceId"], source_id)

    def test_returning_to_the_same_saved_api_source_skips_a_redundant_probe(self):
        request = {"mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
                   "model": "test-model", "apiKey": "test-only-key", "contextTokens": 128000}
        first = self.backend.model_source_activate(request)["sourceId"]
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 1)
        self.backend.model_source_activate({"mode": "local"})
        second = self.backend.model_source_activate({**request, "apiKey": ""})["sourceId"]
        self.assertEqual(second, first)
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 1)
        self.backend.model_source_activate({**request, "model": "different-model", "apiKey": ""})
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 2)

    @staticmethod
    def request(server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=encoded,
                               headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_encrypted_profile_masks_key_and_reuse_is_endpoint_scoped(self):
        source_id = self.store.save_api("responses", "https://example.test/v1", "test-model",
                                        "test-only-key", context_tokens=128000)
        self.assertEqual(len(source_id), 32)
        self.assertNotIn("test-only-key", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.saved_selection()["selectedMode"], "api")
        self.assertEqual(self.store.public()["api"], {
            "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "test-model", "contextTokens": 128000, "hasKey": True})
        self.assertEqual(self.store.public()["mode"], "local")
        self.assertEqual(self.store.resolve_key("responses", "https://example.test/v1", None), "test-only-key")
        self.assertIsNone(self.store.resolve_key("responses", "https://other.test/v1", None))
        self.assertIsNone(self.store.resolve_key("anthropic", "https://example.test/v1", None))
        self.store.save_local()
        self.assertEqual(self.store.saved_selection()["selectedMode"], "local")
        self.assertTrue(self.store.public()["api"]["hasKey"])
        self.store.clear_key()
        self.assertFalse(self.store.public()["api"]["hasKey"])

    def test_url_and_input_validation(self):
        good = connection_values({"protocol": "ollama", "baseUrl": "http://127.0.0.1:11434/"})
        self.assertEqual(good["baseUrl"], "http://127.0.0.1:11434")
        for bad in ("file:///tmp/model", "https://key@example.test/v1", "https://example.test/v1?key=x",
                    "https://example.test/v1#fragment", "https://example.test/../v1",
                    "https://example.test\\@evil.test", "https://example.test\n.evil.test"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                connection_values({"protocol": "responses", "baseUrl": bad})
        with self.assertRaises(ValueError):
            connection_values({"protocol": ["responses"], "baseUrl": "https://example.test/v1"})

    def test_context_size_validation_and_legacy_saved_profile(self):
        request = {"protocol": "responses", "baseUrl": "https://example.test/v1",
                   "model": "test-model"}
        for value in (4096, 1000000):
            with self.subTest(value=value):
                self.assertEqual(connection_values({**request, "contextTokens": value},
                                                   require_model=True)["contextTokens"], value)
        for value in (4095, 1000001, True, 8192.5, "8192"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                connection_values({**request, "contextTokens": value}, require_model=True)
        self.store.save_api("responses", "https://example.test/v1", "test-model",
                            "test-only-key")
        self.assertIsNone(self.store.public()["api"]["contextTokens"])

    def test_api_source_identity_survives_switches_restart_and_key_clear(self):
        def activate(model, key, context_tokens=8192, base_url="https://example.test/v1"):
            return self.backend.model_source_activate({
                "mode": "api", "protocol": "responses", "baseUrl": base_url,
                "model": model, "apiKey": key, "contextTokens": context_tokens})["sourceId"]

        first = activate("model-a", "synthetic-key-a")
        second = activate("model-b", "synthetic-key-b")
        self.assertNotEqual(first, second)
        self.assertEqual(activate("model-a", "synthetic-key-a", 16384), first)
        changed_key = activate("model-a", "synthetic-key-c")
        self.assertEqual(changed_key, first)
        changed_endpoint = activate("model-a", "synthetic-key-a",
                                    base_url="https://other.test/v1")
        self.assertNotIn(changed_endpoint, (first, second))

        reopened = ModelSourceStore(
            self.path, root=self.root,
            protect=lambda key: b"protected:" + key[::-1].encode("utf-8"),
            unprotect=lambda data: data.removeprefix(b"protected:").decode("utf-8")[::-1])
        self.backend.model_source_store = reopened
        self.assertEqual(activate("model-b", "synthetic-key-b"), second)
        self.backend.model_source_clear_key({})
        keyless = activate("model-b", "")
        self.assertEqual(keyless, second)
        self.assertEqual(activate("model-a", "synthetic-key-a"), first)
        public = self.backend.model_source()
        self.assertNotIn("sourceIds", public)
        stored = self.path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-key-a", stored)
        self.assertNotIn("synthetic-key-b", stored)
        self.assertNotIn("synthetic-key-c", stored)

    def test_legacy_api_source_id_migrates_before_switch(self):
        first = self.store.save_api("responses", "https://example.test/v1", "model-a",
                                    "synthetic-key-a", context_tokens=8192)
        old = json.loads(self.path.read_text(encoding="utf-8"))
        old.pop("sourceIds")
        self.legacy.write_text(json.dumps(old), encoding="utf-8")
        self.path.unlink()
        legacy_bytes = self.legacy.read_bytes()
        self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "model-b", "apiKey": "synthetic-key-b", "contextTokens": 8192})
        restored = self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "model-a", "apiKey": "synthetic-key-a", "contextTokens": 16384})
        self.assertEqual(restored["sourceId"], first)
        self.assertEqual(self.legacy.read_bytes(), legacy_bytes)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["sourceIds"]), 2)

    def test_legacy_local_selection_keeps_api_endpoint_available(self):
        selected = self.root / "synthetic-model"
        selected.mkdir()
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text(json.dumps({"schema": 1, "path": str(selected)}), encoding="utf-8")
        original = self.legacy.read_bytes()
        with mock.patch("local_model_source.available_model_dir",
                        side_effect=lambda path: Path(path) == selected):
            self.assertEqual(self.local_source.status()["path"], str(selected))
            self.assertEqual(self.request(self.server(), "GET", "/api/model-source"),
                             (200, {"mode": "local", "api": None,
                                    "sourceId": LOCAL_SOURCE_ID, "status": "active"}))
            self.store.save_api("responses", "https://example.test/v1", "test-model",
                                "synthetic-key", context_tokens=128000)
            self.assertEqual(self.local_source.status()["path"], str(selected))
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertTrue(self.path.is_file())
        self.assertNotIn("synthetic-key", self.path.read_text(encoding="utf-8"))

    def test_legacy_api_profile_and_local_selection_survive_source_switches(self):
        self.store.save_api("responses", "https://example.test/v1", "test-model",
                            "synthetic-key", context_tokens=128000)
        self.legacy.write_bytes(self.path.read_bytes())
        self.path.unlink()
        original = self.legacy.read_bytes()
        self.assertEqual(self.store.saved_selection()["selectedMode"], "api")
        self.assertTrue(self.store.public()["api"]["hasKey"])
        self.assertIsNone(self.local_source._selected_path())
        selected = self.root / "synthetic-model"
        selected.mkdir()
        with mock.patch("local_model_source.validate_model_dir", return_value=selected), \
                mock.patch("local_model_source.available_model_dir",
                           side_effect=lambda path: Path(path) == selected):
            self.assertEqual(self.local_source.select(str(selected))["source"], "custom")
            activated = self.backend.model_source_activate({
                "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
                "model": "test-model", "contextTokens": 128000, "apiKey": ""})
            self.assertEqual(activated["mode"], "api")
            self.assertTrue(activated["api"]["hasKey"])
            self.assertEqual(self.backend.model_source_activate({"mode": "local"})["mode"], "local")
            self.assertEqual(self.local_source.status()["path"], str(selected))
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertTrue(self.path.is_file())
        self.assertTrue(self.local_source.config.is_file())
        self.assertEqual(self.store.saved_selection()["selectedMode"], "local")
        self.assertEqual(self.store.resolve_key("responses", "https://example.test/v1", None),
                         "synthetic-key")

    def test_config_paths_reject_escape(self):
        outside = self.root.parent / "outside-model-source.json"
        unsafe = ModelSourceStore(outside, root=self.root, legacy_path=self.legacy)
        with self.assertRaises(ModelSourceUnavailable):
            unsafe.saved_selection()
        with self.assertRaises(ModelSourceUnavailable):
            unsafe.save_api("responses", "https://example.test/v1", "test-model", None)
        self.local_source.config = outside
        with mock.patch("local_model_source.validate_model_dir", return_value=self.root):
            with self.assertRaises(ModelBundleError):
                self.local_source.select(str(self.root))

    def test_config_paths_reject_reparse_directory(self):
        runtime = self.path.parent
        runtime.mkdir(parents=True)
        real_lstat = Path.lstat
        def marked_lstat(path):
            state = real_lstat(path)
            if path == runtime:
                return SimpleNamespace(st_mode=state.st_mode, st_file_attributes=0x400)
            return state
        with mock.patch.object(Path, "lstat", marked_lstat):
            with self.assertRaises(ModelSourceUnavailable):
                self.store.saved_selection()
            with mock.patch("local_model_source.validate_model_dir", return_value=self.root):
                with self.assertRaises(ModelBundleError):
                    self.local_source.select(str(self.root))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.local_source.config.exists())

    def test_http_contract_probes_and_activation(self):
        server = self.server()
        status, initial = self.request(server, "GET", "/api/model-source")
        self.assertEqual(status, 200)
        self.assertEqual(initial, {"mode": "local", "api": None,
                                   "sourceId": LOCAL_SOURCE_ID, "status": "active"})
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1",
                    "apiKey": "test-only-key"}
        status, models = self.request(server, "POST", "/api/model-source/list", settings)
        self.assertEqual((status, models), (200, {"models": [{"id": "test-model", "name": "Test Model"}],
                                                  "supported": True}))
        status, tested = self.request(server, "POST", "/api/model-source/test",
                                      {**settings, "model": "test-model"})
        self.assertEqual((status, tested), (200, {"ok": True, "latencyMs": 12.5}))
        self.assertEqual(self.backend.analyzer.calls[0][-1], "test-only-key")
        status, missing = self.request(server, "POST", "/api/model-source/activate",
                                       {"mode": "api", **settings, "model": "test-model"})
        self.assertEqual(status, 400)
        self.assertEqual(self.backend.active_model_source_mode, "local")
        status, active = self.request(server, "POST", "/api/model-source/activate",
                                      {"mode": "api", **settings, "model": "test-model",
                                       "contextTokens": 128000})
        self.assertEqual(status, 200)
        self.assertEqual(active["mode"], "api")
        self.assertEqual(active["api"]["contextTokens"], 128000)
        self.assertEqual(len(active["sourceId"]), 32)
        self.assertNotIn("test-only-key", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.request(server, "POST", "/api/model-source/clear-key", {})[0], 200)
        self.assertEqual(self.backend.active_model_source_mode, "local")
        self.assertEqual(self.request(server, "POST", "/api/model-source/list",
                                      {**settings, "protocol": "unknown"})[0], 400)
        self.assertEqual(self.request(server, "POST", "/api/model-source/list", settings,
                                      {"Origin": "https://evil.test"})[0], 403)

    def test_connector_auth_error_is_reported_without_provider_body(self):
        server = self.server()
        def denied(*_args):
            raise RuntimeError("auth")
        self.backend.analyzer.model_test = denied
        status, body = self.request(server, "POST", "/api/model-source/test", {
            "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "test-model", "apiKey": "test-only-key"})
        self.assertEqual((status, body), (503, {"error": "auth"}))
        self.assertEqual(self.backend.active_model_source_mode, "local")


if __name__ == "__main__":
    unittest.main()

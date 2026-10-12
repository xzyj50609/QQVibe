"""Model-source security and HTTP contract checks without provider or chat access."""

import base64
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
from api_pool import ApiAnalyzerPool
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


class ModelSourceTestCase(unittest.TestCase):
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
        self.backend.source = SimpleNamespace()
        self.backend.message_jobs_lock = threading.RLock()
        self.backend.message_jobs = {}
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
        self.backend.active_model_profile_id = None
        self.backend.api_guidance_jobs = self.backend.api_tasks.guidance_jobs
        self.backend.api_pool = ApiAnalyzerPool(lambda: self.backend.api_analyzer,
            factory=FakeAnalyzer, settings_path=self.path.parent / 'api-workers.json')
        self.addCleanup(self.backend.api_pool.close_extras)
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
        switched = self.backend.model_source_activate({"mode": "local"})
        self.assertEqual((switched["mode"], switched["sourceId"], switched["label"]),
                         ("local", LOCAL_SOURCE_ID, "本地 Laya"))
        self.assertIsNone(self.backend.active_api_config)
        self.assertEqual(switched["api"]["model"], "test-model")
        self.assertEqual((insight.cancelled, portrait.cancelled), (1, 1))
        self.assertEqual(self.backend.api_jobs, {})
        self.assertEqual(self.backend.api_portrait_jobs, {})
        self.assertEqual(self.store.saved_selection()["sourceId"], source_id)

    def test_returning_to_the_same_saved_api_source_skips_a_redundant_probe(self):
        request = {"mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
                   "model": "test-model", "apiKey": "test-only-key", "contextTokens": 128000}
        first = self.backend.model_source_activate(request)["sourceId"]
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 1)
        local = self.backend.model_source_activate({"mode": "local"})
        self.assertEqual(local["label"], "本地 Laya")
        self.assertEqual(local["api"]["model"], "test-model")
        restored = self.backend.model_source_activate({**request, "apiKey": ""})
        self.assertEqual((restored["mode"], restored["label"]), ("api", "test-model"))
        second = restored["sourceId"]
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
            "id": source_id, "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "test-model", "contextTokens": 128000, "hasKey": True})
        self.assertEqual(self.store.public()["mode"], "local")
        self.assertEqual(self.store.public()["label"], "本地 Laya")
        self.assertEqual(self.store.public(mode="api", source_id=source_id)["label"], "test-model")
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
                             (200, {"mode": "local", "api": None, "profiles": [],
                                    "label": "本地 Laya",
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
        self.assertEqual(initial, {"mode": "local", "api": None, "profiles": [],
                                   "label": "本地 Laya",
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


class CancellableAnalyzer(FakeAnalyzer):
    def __init__(self):
        super().__init__()
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


class ModelProfileTests(ModelSourceTestCase):
    def test_probe_uses_the_named_profile_key_even_when_another_profile_is_active(self):
        first = self.backend.model_source_activate({"mode": "api", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a", "apiKey": "key-a", "contextTokens": 8192})
        second = self.backend.model_source_profile_save({"name": "B", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-b", "apiKey": "key-b", "contextTokens": 8192})
        self.backend.model_source_test({"profileId": second["profile"], "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-b", "contextTokens": 8192})
        self.assertEqual(self.backend.api_probe_analyzer.calls[-1][3], "key-b")
        self.assertEqual(self.backend.model_source()["sourceId"], first["sourceId"])
        self.backend.model_source_test({"protocol": "responses", "baseUrl": "https://example.test/v1",
                                       "model": "draft", "contextTokens": 8192})
        self.assertIsNone(self.backend.api_probe_analyzer.calls[-1][3])

    def test_editing_model_changes_cache_identity_without_changing_profile_id(self):
        first = self.backend.model_source_activate({"mode": "api", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a", "apiKey": "key-a", "contextTokens": 8192})
        profile_id = first["api"]["id"]
        request = {"profileId": profile_id, "name": "A", "protocol": "responses",
                   "baseUrl": "https://example.test/v1", "model": "model-b", "contextTokens": 8192}
        edited = self.backend.model_source_profile_save(request)
        self.assertEqual(edited["api"]["id"], profile_id)
        self.assertNotEqual(edited["sourceId"], first["sourceId"])
        restored = self.backend.model_source_profile_save({**request, "model": "model-a"})
        self.assertEqual(restored["sourceId"], first["sourceId"])

    """Multiple saved API profiles: create, edit, switch and delete."""

    def activate(self, model, base_url="https://example.test/v1", key="test-only-key",
                 context_tokens=128000, name=None):
        payload = {"mode": "api", "protocol": "responses", "baseUrl": base_url,
                   "model": model, "apiKey": key, "contextTokens": context_tokens}
        if name is not None:
            payload["name"] = name
        return self.backend.model_source_activate(payload)

    def test_saving_a_second_profile_keeps_the_first_and_leaves_the_selection_alone(self):
        first = self.activate("model-a", name="线路 A")
        second = self.activate("model-b", base_url="https://other.test/v1", name="线路 B")
        self.assertNotEqual(first["sourceId"], second["sourceId"])
        saved = self.backend.model_source_profile_save({
            "profileId": first["api"]["id"], "name": "线路 A 改名", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a",
            "contextTokens": 200000})
        self.assertEqual(saved["profile"], first["api"]["id"])
        self.assertEqual([item["name"] for item in saved["profiles"]], ["线路 A 改名", "线路 B"])
        # Editing a profile that is not active must not move the conversation onto it.
        self.assertEqual(saved["sourceId"], second["sourceId"])
        self.assertEqual(saved["api"]["model"], "model-b")

    def test_switching_by_profile_id_reuses_the_stored_key_without_a_new_probe(self):
        first = self.activate("model-a", name="线路 A")
        self.activate("model-b", name="线路 B")
        probes = len(self.backend.api_probe_analyzer.calls)
        switched = self.backend.model_source_activate({"mode": "api", "profileId": first["api"]["id"]})
        self.assertEqual(switched["sourceId"], first["sourceId"])
        self.assertEqual(switched["label"], "线路 A")
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), probes)
        self.assertEqual(self.backend.active_api_config["model"], "model-a")
        self.assertEqual(self.backend.model_source_store.resolve_key(
            "responses", "https://example.test/v1", None), "test-only-key")
        with self.assertRaises(ValueError):
            self.backend.model_source_activate({"mode": "api", "profileId": "0" * 32})
        with self.assertRaises(ValueError):
            self.backend.model_source_activate({"mode": "api", "profileId": first["api"]["id"],
                                                "model": "model-c"})

    def test_activate_matches_an_existing_connection_instead_of_adding_one(self):
        """Why the settings form saves before switching.

        `activate` with connection fields and no `profileId` looks the connection up by
        protocol/baseUrl/model and updates that profile in place, so a second entry for an
        already-saved connection never becomes a new row in the model list. `save_profile`
        (what `/api/model-source/profiles` calls) always appends for a new profile.
        """
        first = self.activate("model-a", name="线路 A")
        again = self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "model-a", "apiKey": "test-only-key", "contextTokens": 128000})
        self.assertEqual(again["sourceId"], first["sourceId"])
        self.assertEqual([item["id"] for item in again["profiles"]], [first["sourceId"]])
        appended = self.backend.model_source_profile_save({
            "name": "线路 A 第二份", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a",
            "apiKey": "test-only-key", "contextTokens": 128000})
        self.assertEqual(len(appended["profiles"]), 2)
        self.assertNotEqual(appended["profile"], first["sourceId"])

    def test_activate_rejects_connection_fields_next_to_a_profile_id(self):
        """Either the connection fields or `{mode, profileId}` — never both.

        `chatui/app.js:saveApiProfileDraft` therefore saves the edits through
        `/api/model-source/profiles` first and then switches with the two-key body. Sending the
        mixed body was answered with 400 `invalid model source request`, which the form reported
        as 启用失败 immediately after a successful 测试连接.
        """
        profile = self.activate("model-a", name="线路 A")
        probes = len(self.backend.api_probe_analyzer.calls)
        with self.assertRaises(ValueError):
            self.backend.model_source_activate({
                "mode": "api", "profileId": profile["api"]["id"], "name": "线路 A",
                "protocol": "responses", "baseUrl": "https://example.test/v1",
                "model": "model-a2", "contextTokens": 128000})
        # Rejected before any probe: the mixed body must not spend a provider round trip.
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), probes)
        # The two-key form is the switch the form falls back to: no probe, same profile.
        switched = self.backend.model_source_activate(
            {"mode": "api", "profileId": profile["api"]["id"]})
        self.assertEqual((switched["sourceId"], switched["label"]),
                         (profile["sourceId"], "线路 A"))
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), probes)

    def test_editing_the_active_profile_republishes_it_to_the_running_worker(self):
        active = self.activate("model-a")
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 1)
        self.backend.model_source_profile_save({
            "profileId": active["api"]["id"], "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a2",
            "contextTokens": 64000})
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 2)
        self.assertEqual(self.backend.active_api_config["model"], "model-a2")
        self.assertEqual(self.backend.active_api_config["contextTokens"], 64000)
        # An unchanged edit of the active profile keeps its validated connection.
        self.backend.model_source_profile_save({
            "profileId": active["api"]["id"], "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a2",
            "contextTokens": 64000})
        self.assertEqual(len(self.backend.api_probe_analyzer.calls), 2)

    def test_deleting_the_active_profile_falls_back_to_local_and_keeps_the_others(self):
        first = self.activate("model-a", name="线路 A")
        second = self.activate("model-b", base_url="https://other.test/v1", name="线路 B")
        cancelled = CancellableAnalyzer()
        portrait = CancellableAnalyzer()
        self.backend.api_analyzer = cancelled
        self.backend.api_portrait_analyzer = portrait
        remaining = self.backend.model_source_profile_delete({"profileId": second["sourceId"]})
        self.assertEqual(remaining["mode"], "local")
        self.assertEqual(remaining["sourceId"], LOCAL_SOURCE_ID)
        self.assertIsNone(remaining["api"])
        self.assertEqual([item["id"] for item in remaining["profiles"]], [first["sourceId"]])
        self.assertEqual((cancelled.cancelled, portrait.cancelled), (1, 1))
        self.assertEqual(self.backend.active_model_source_mode, "local")
        with self.assertRaises(ValueError):
            self.backend.model_source_profile_delete({"profileId": second["sourceId"]})

    def test_clearing_one_key_leaves_the_active_source_alone(self):
        first = self.activate("model-a", name="线路 A")
        second = self.activate("model-b", base_url="https://other.test/v1", name="线路 B")
        kept = self.backend.model_source_clear_key({"profileId": first["sourceId"]})
        self.assertEqual(kept["mode"], "api")
        self.assertEqual(kept["sourceId"], second["sourceId"])
        self.assertFalse(kept["profiles"][0]["hasKey"])
        self.assertTrue(kept["profiles"][1]["hasKey"])
        self.assertEqual(kept["label"], "线路 B")
        with self.assertRaises(ValueError):
            self.backend.model_source_clear_key({"profileId": "0" * 32})

    def test_a_blank_key_never_carries_a_saved_secret_to_another_endpoint(self):
        first = self.activate("model-a", name="线路 A")
        moved = self.backend.model_source_profile_save({
            "profileId": first["api"]["id"], "protocol": "responses",
            "baseUrl": "https://other.test/v1", "model": "model-a",
            "contextTokens": 128000})
        self.assertFalse(moved["profiles"][0]["hasKey"])
        stored = self.path.read_text(encoding="utf-8")
        self.assertNotIn("test-only-key", stored)

    def test_http_contract_manages_profiles(self):
        server = self.server()
        status, saved = self.request(server, "POST", "/api/model-source/profiles", {
            "name": "线路 A", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "model-a", "apiKey": "test-only-key", "contextTokens": 128000})
        self.assertEqual(status, 200)
        self.assertEqual(len(saved["profile"]), 32)
        # Saving alone must not activate: the conversation keeps running locally.
        self.assertEqual((saved["mode"], saved["sourceId"]), ("local", LOCAL_SOURCE_ID))
        self.assertEqual(saved["label"], "本地 Laya")
        status, listed = self.request(server, "POST", "/api/model-source/profiles", {
            "profileId": saved["profile"], "name": "线路 A", "protocol": "responses",
            "baseUrl": "https://example.test/v1", "model": "model-a",
            "apiKey": "test-only-key", "contextTokens": 128000})
        self.assertEqual((status, listed["profile"]), (200, saved["profile"]))
        status, active = self.request(server, "POST", "/api/model-source/activate",
                                      {"mode": "api", "profileId": saved["profile"]})
        self.assertEqual((status, active["mode"], active["label"]), (200, "api", "线路 A"))
        self.assertEqual(self.request(server, "POST", "/api/model-source/profiles/delete",
                                      {"profileId": "0" * 32})[0], 400)
        status, deleted = self.request(server, "POST", "/api/model-source/profiles/delete",
                                       {"profileId": saved["profile"]})
        self.assertEqual((status, deleted["mode"], deleted["profiles"]), (200, "local", []))
        self.assertEqual(self.request(server, "POST", "/api/model-source/profiles",
                                      {"protocol": "responses"})[0], 400)

    def test_profile_limit_and_version_one_migration(self):
        legacy = {"version": 1, "sourceId": "a" * 32, "selectedMode": "api",
                  "api": {"protocol": "responses", "baseUrl": "https://example.test/v1",
                          "model": "model-a", "contextTokens": 128000,
                          "encryptedKey": base64.b64encode(
                              self.store.protect("legacy-key")).decode("ascii")}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = self.store.public()
        self.assertEqual(migrated["label"], "本地 Laya")
        self.assertEqual(self.store.public(mode="api", source_id="a" * 32)["label"], "model-a")
        self.assertEqual(migrated["profiles"][0]["name"], "model-a")
        self.assertEqual(self.store.resolve_key("responses", "https://example.test/v1", None),
                         "legacy-key")
        self.assertEqual(self.store.saved_selection()["sourceId"], "a" * 32)
        # The migrated profile occupies one of the twenty slots the store may hold.
        for index in range(19):
            with self.subTest(index=index):
                self.store.save_profile(None, f"线路 {index}", "responses",
                                        f"https://synthetic{index}.test/v1", "model-a",
                                        None, 128000)
        self.assertEqual(len(self.store.public()["profiles"]), 20)
        with self.assertRaises(ValueError):
            self.store.save_profile(None, "线路 19", "responses",
                                    "https://overflow.test/v1", "model-a", None, 128000)
        self.assertEqual(len(self.store.public()["profiles"]), 20)
        with self.assertRaises(ValueError):
            self.store.save_profile("0" * 32, "线路 20", "responses",
                                    "https://missing.test/v1", "model-a", None, 128000)


class DpapiProfileTests(unittest.TestCase):
    """The real Windows DPAPI key path, without the injected test protector.

    Deliberately standalone: the shared fixture swaps in a reversible fake protector,
    so inheriting it would both drag in unrelated cases and defeat the point here.
    """

    def setUp(self):
        try:
            import win32crypt  # noqa: F401
        except ImportError:
            self.skipTest("pywin32 is unavailable; DPAPI keys cannot be exercised")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "api-model-source.json"
        self.store = ModelSourceStore(self.path, root=self.root)

    def test_keys_round_trip_through_dpapi_across_profiles_and_restarts(self):
        first = self.store.save_profile(None, "线路 A", "responses", "https://a.test/v1",
                                        "model-a", "sk-real-aaaa", 128000)
        second = self.store.save_profile(None, "线路 B", "responses", "https://b.test/v1",
                                         "model-b", "sk-real-bbbb", 64000)
        stored = self.path.read_text(encoding="utf-8")
        for secret in ("sk-real-aaaa", "sk-real-bbbb"):
            self.assertNotIn(secret, stored)
        self.assertTrue(self.store.public()["profiles"][0]["hasKey"])
        self.assertEqual(self.store.resolve_key("responses", "https://a.test/v1", None),
                         "sk-real-aaaa")
        self.assertEqual(self.store.resolve_key("responses", "https://b.test/v1", None),
                         "sk-real-bbbb")
        # A fresh store stands in for the next launch of the application: this only
        # decrypts if the blob on disk really is DPAPI ciphertext.
        reopened = ModelSourceStore(self.path, root=self.root)
        self.assertEqual(reopened.resolve_key("responses", "https://a.test/v1", None),
                         "sk-real-aaaa")
        self.assertFalse(reopened.clear_key(second))
        self.assertIsNone(reopened.resolve_key("responses", "https://b.test/v1", None))
        self.assertEqual(reopened.resolve_key("responses", "https://a.test/v1", None),
                         "sk-real-aaaa")
        self.assertFalse(reopened.delete_profile(first))
        self.assertEqual([item["name"] for item in reopened.public()["profiles"]], ["线路 B"])
        # The second profile was never the selected one, so removing it reports no
        # fallback; the saved selection already fell back when the first was deleted.
        self.assertFalse(reopened.delete_profile(second))
        self.assertEqual(reopened.public()["profiles"], [])
        self.assertEqual(reopened.saved_selection()["selectedMode"], "local")



if __name__ == "__main__":
    unittest.main()

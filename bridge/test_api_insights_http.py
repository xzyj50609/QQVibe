"""Synthetic loopback check across HTTP, Python, Node, and the OpenAI SDK."""

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from model_source import ModelSourceStore
from real_backend import Backend, ResultStore
from real_http import make_handler


class FakeProvider(BaseHTTPRequestHandler):
    prompts = []

    def log_message(self, *_args):
        pass

    def reply(self, value):
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self.reply({"object": "list", "data": [{"id": "synthetic-model", "object": "model",
                                                "created": 0, "owned_by": "fixture"}]})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][1]["content"]
        self.prompts.append(prompt)
        if prompt == 'Reply with JSON {"ok":true}.':
            answer = '{"ok":true}'
        elif prompt.startswith("CHAT_BATCH_JSON:\n"):
            payload = json.loads(prompt.removeprefix("CHAT_BATCH_JSON:\n"))
            answer = json.dumps({"items": [{
                "id": target_id, "status": "ok", "emotion": "期待", "intent": "邀约",
            } for target_id in payload["targetIds"]]}, ensure_ascii=False)
        else:
            answer = json.dumps({"summary": "常讨论周末见面", "communication": "表达简洁",
                                     "emotionExpression": "", "interactionPreferences": "",
                                     "topics": ["周末"], "patterns": [], "boundaries": [],
                                     "uncertain": [], "affinity": None,
                                     "mbtiAxes": {key: None for key in ("EI", "SN", "TF", "JP")},
                                     "traits": {key: None for key in ("socialEnergy", "humor", "composure",
                                                                         "initiative", "care", "affection")}},
                                    ensure_ascii=False)
        self.reply({"id": "synthetic-response", "object": "chat.completion", "created": 0,
                    "model": "synthetic-model", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]})


class Source:
    def __init__(self, root):
        self.root = root
        self.rows = [
            {"id": "s1", "side": "self", "kind": "text", "text": "周六看展吗？",
             "senderId": "me", "_sort": [1, "shard", 1]},
            {"id": "o1", "side": "other", "kind": "text", "text": "可以呀，我来找你。",
             "senderId": "friend", "_sort": [2, "shard", 2]},
            {"id": "s2", "side": "self", "kind": "text", "text": "那周六见。",
             "senderId": "me", "_sort": [3, "shard", 3]},
        ]

    def identity(self):
        return "synthetic-account", str(self.root)

    def contact(self, _user):
        return {"name": "合成联系人", "avatar": "", "avatarCandidates": []}

    def profile_metadata(self, user, member=None):
        if member is not None:
            raise ValueError("unknown member")
        return {"contact": {"name": "合成联系人", "avatar": "", "avatarCandidates": []},
                "members": [], "count": len(self.rows), "textCount": len(self.rows)}

    def messages(self, _user, limit):
        return self.rows[-limit:]

    def history_highwater(self, _user):
        return tuple(self.rows[-1]["_sort"])

    def history_page(self, _user, highwater, after=None, page_size=256):
        rows = [item for item in self.rows if tuple(item["_sort"]) <= highwater and
                (after is None or tuple(item["_sort"]) > after)][:page_size]
        return rows, tuple(rows[-1]["_sort"]) if rows else None


class ApiInsightHttpTests(unittest.TestCase):
    def test_synthetic_provider_through_public_http_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            FakeProvider.prompts = []
            provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
            provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
            provider_thread.start()
            source = Source(root)
            store = ModelSourceStore(root / "model-source.json", root=root,
                                     protect=lambda value: b"sealed:" + value.encode(),
                                     unprotect=lambda value: value.removeprefix(b"sealed:").decode())
            backend = Backend(source, model_source_store=store,
                              store_factory=lambda account, _workdir: ResultStore(root / f"{account}.sqlite3"))
            app = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend))
            app_thread = threading.Thread(target=app.serve_forever, daemon=True)
            app_thread.start()
            base = f"http://127.0.0.1:{app.server_port}"
            provider_base = f"http://127.0.0.1:{provider.server_port}/v1"

            def call(path, value=None):
                body = None if value is None else json.dumps(value).encode("utf-8")
                request = Request(base + path, data=body,
                                  headers={"Content-Type": "application/json", "Origin": base})
                with urlopen(request, timeout=15) as response:
                    return response.status, json.load(response)

            try:
                config = {"protocol": "chat_completions", "baseUrl": provider_base,
                          "apiKey": "synthetic-key", "model": "synthetic-model",
                          "contextTokens": 8192}
                self.assertEqual(call("/api/model-source/list", {key: config[key]
                                 for key in ("protocol", "baseUrl", "apiKey")})[1]["models"][0]["id"],
                                 "synthetic-model")
                self.assertTrue(call("/api/model-source/test", config)[1]["ok"])
                self.assertEqual(call("/api/model-source/activate", {"mode": "api", **config})[1]["mode"],
                                 "api")
                self.assertNotIn("synthetic-key", (root / "model-source.json").read_text())
                status, started = call("/api/model-insights", {"account": "synthetic-account",
                                                               "user": "friend", "limit": 1,
                                                               "targetIds": ["o1"]})
                self.assertEqual(status, 202)
                self.assertEqual(started["job"]["total"], 1)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    _status, result = call("/api/model-insights?user=friend")
                    if result["job"]["status"] in ("done", "error"):
                        break
                    time.sleep(.05)
                self.assertEqual(result["job"]["status"], "done", result["job"])
                self.assertEqual(result["results"]["o1"],
                                 {"id": "o1", "status": "ok", "affect": {"feeling": "期待"}, "intents": ["邀约"]})
                self.assertEqual(len(FakeProvider.prompts), 3)  # test, activate, insight
                status, portrait_job = call("/api/model-portrait", {
                    "account": "synthetic-account", "user": "friend"})
                self.assertEqual(status, 202)
                self.assertIn(portrait_job["job"]["status"], ("queued", "running"))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    _status, portrait = call("/api/model-portrait?user=friend")
                    if portrait["job"]["status"] in ("done", "error"):
                        break
                    time.sleep(.05)
                self.assertEqual(portrait["job"]["status"], "done", portrait)
                self.assertTrue(portrait["progress"]["complete"])
                self.assertEqual(portrait["portrait"]["summary"], "常讨论周末见面")
                self.assertEqual(portrait["available"]["textCount"], 3)
                self.assertEqual(portrait["progress"]["processed"], 3)
                _status, cache = call("/api/analysis-cache")
                api_source = next(item for item in cache["sources"] if item["kind"] == "api")
                self.assertEqual((api_source["messageCount"], api_source["portraitCount"]), (1, 1))
                _status, cleared = call("/api/analysis-cache/clear", {
                    "account": "synthetic-account", "sourceId": api_source["sourceId"]})
                self.assertTrue(cleared["cleared"])
                _status, cache = call("/api/analysis-cache")
                api_source = next(item for item in cache["sources"] if item["kind"] == "api")
                self.assertEqual((api_source["messageCount"], api_source["portraitCount"]), (0, 0))
                self.assertFalse(api_source["suspended"])
            finally:
                app.shutdown()
                app.server_close()
                backend.shutdown()
                provider.shutdown()
                provider.server_close()


if __name__ == "__main__":
    unittest.main()

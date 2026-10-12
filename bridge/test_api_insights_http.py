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
from portrait_contracts import api_portrait_scope
from api_portrait_statistics import valid_statistics


class FakeProvider(BaseHTTPRequestHandler):
    prompts = []
    portrait_phases = []
    on_classification = None

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
        elif prompt.startswith("INPUT_JSON:\n"):
            payload = json.loads(prompt.removeprefix("INPUT_JSON:\n"))
            system = next(message["content"] for message in body["messages"]
                          if message["role"] == "system")
            if "messages" not in payload or "LOCAL_QUESTION_CONTRACT:\n" not in system:
                self.send_error(400, "expected local-question batch classification")
                return
            self.portrait_phases.append(("classify", payload))
            if type(self).on_classification is not None:
                type(self).on_classification()
            contract = json.loads(system.split("LOCAL_QUESTION_CONTRACT:\n", 1)[1])
            answers = {}
            for name, (_instruction, labels) in contract["questions"].items():
                selected = labels.index("warm") if name == "relationship" else 0
                answers[name] = [int(index == selected) for index in range(len(labels))]
            # The local router keeps only selected branch mass (.7 here), and
            # ordinary chat legitimately produces no enduring MBTI preference.
            answers["emotion"] = [.4, .3, .3] + [0] * (len(answers["emotion"]) - 3)
            answer = json.dumps({"answers": answers}, ensure_ascii=False)
        else:
            self.send_error(400, "unexpected synthetic prompt")
            return
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
            FakeProvider.portrait_phases = []
            FakeProvider.on_classification = None
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
                          "contextTokens": 32768}
                self.assertEqual(call("/api/model-source/list", {key: config[key]
                                 for key in ("protocol", "baseUrl", "apiKey")})[1]["models"][0]["id"],
                                 "synthetic-model")
                self.assertTrue(call("/api/model-source/test", config)[1]["ok"])
                selected = call("/api/model-source/activate", {"mode": "api", **config})[1]
                self.assertEqual(selected["mode"], "api")
                checkpoints = []
                result_store = ResultStore(root / "synthetic-account.sqlite3")
                def capture_pending_checkpoint():
                    checkpoints.append(result_store.api_portrait_get("synthetic-account", "friend",
                        api_portrait_scope(selected["sourceId"]), "friend"))
                FakeProvider.on_classification = capture_pending_checkpoint
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
                profile = portrait["nativeProfile"]
                self.assertTrue(profile["summary"].startswith("已分析1条该联系人消息"))
                self.assertEqual(portrait["available"]["textCount"], 3)
                self.assertEqual(portrait["progress"]["processed"], 3)
                self.assertEqual(portrait["progress"]["processedTargetTexts"], 1)
                self.assertFalse(portrait["needsRebuild"])
                self.assertEqual(profile["affinity"], 78)
                self.assertIsNone(profile["mbti"])
                self.assertEqual(profile["portraitCount"], 1)
                self.assertEqual(profile["mbtiInference"]["eligibleMessages"], 1)
                self.assertEqual(profile["mbtiInference"]["supportedMessages"], 0)
                self.assertTrue(all(axis["leftShare"] is None and axis["insufficientCount"] == 1
                                    for axis in profile["mbtiInference"]["axes"].values()))
                self.assertEqual(len(profile["traits"]), 6)
                self.assertTrue(all(item["sampleCount"] == 1 for item in profile["traits"]))
                self.assertEqual([phase for phase, _payload in FakeProvider.portrait_phases],
                                 ["classify"])
                classify_input = FakeProvider.portrait_phases[0][1]
                self.assertEqual(set(classify_input), {"subjectKind", "messages"})
                self.assertEqual([item["text"] for item in classify_input["messages"] if item["target"]],
                                 ["可以呀，我来找你。"])
                self.assertEqual(len(checkpoints), 1)
                self.assertFalse(checkpoints[0]["complete"], "a pending model request cannot count as analyzed")
                self.assertEqual((checkpoints[0]["processed"], checkpoints[0]["batchIndex"]), (0, 0))
                self.assertEqual(checkpoints[0]["resume"]["portraitStatistics"]["state"]["targetCount"], 0)
                saved = result_store.api_portrait_get("synthetic-account", "friend",
                    api_portrait_scope(selected["sourceId"]), "friend")
                self.assertTrue(saved["complete"])
                self.assertEqual((saved["processed"], saved["batchIndex"], saved["highwater"]), (3, 1, (3, "shard", 3)))
                self.assertEqual(saved["available"]["processedTargetTextCount"], 1)
                statistics = saved["resume"]["portraitStatistics"]
                self.assertTrue(valid_statistics(statistics))
                self.assertEqual((statistics["state"]["targetCount"], statistics["state"]["batchCount"]), (1, 1))
                self.assertAlmostEqual(sum(statistics["state"]["emotion"].values()), .7)
                self.assertEqual(statistics["state"]["supported"], 0)
                self.assertEqual(saved["resume"]["batchIndex"], saved["batchIndex"])
                self.assertIn("tailHash", saved["resume"])
                request_count = len(FakeProvider.prompts)
                call("/api/model-portrait", {"account": "synthetic-account", "user": "friend"})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    _status, repeated = call("/api/model-portrait?user=friend")
                    if repeated["job"]["status"] in ("done", "error"):
                        break
                    time.sleep(.05)
                self.assertEqual(repeated["job"]["status"], "done")
                self.assertEqual(len(FakeProvider.prompts), request_count)
                self.assertEqual(repeated["nativeProfile"]["mbtiInference"], profile["mbtiInference"])
                self.assertEqual(result_store.api_portrait_get("synthetic-account", "friend",
                    api_portrait_scope(selected["sourceId"]), "friend"), saved)
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
                FakeProvider.on_classification = None
                app.shutdown()
                app.server_close()
                backend.shutdown()
                provider.shutdown()
                provider.server_close()


if __name__ == "__main__":
    unittest.main()

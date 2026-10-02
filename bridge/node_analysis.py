"""Node inference-process adapter; owns process IO and model response validation.

Importing this module does not launch Node or load a model.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from backend_contracts import ROOT, valid_api_portrait, validate_personality_evidence
from local_model_source import ModelSource
from product_profile import current_product
from profile_signals import validate_style_evidence

PRODUCT = current_product()


class NodeAnalysis:
    def __init__(self, settings_path=None, *, api_only=False):
        self.condition = threading.Condition()
        self.process = None
        self.reader_thread = None
        self.pending = {}
        # Streaming model output is delivered as an out-of-band JSONL event.  Keep
        # callbacks separate from ``pending`` so a partial event can never satisfy
        # the request before the final validated reply arrives.
        self.stream_callbacks = {}
        self.expired_request_ids = set()
        self.model = {"state": "idle"}
        self.serial = 0
        self.version = None
        self.running_version = None
        self.api_only = api_only
        self.settings_path = Path(settings_path) if settings_path is not None else (
            PRODUCT.state_dir("real-client-runtime", ROOT) / "inference-settings.json")
        self.requested_provider = self._read_provider()
        self.local_model_source = ModelSource(ROOT)

    def _read_provider(self):
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8")).get("provider")
        except (FileNotFoundError, OSError, ValueError, AttributeError):
            return "gpu"
        return value if value in ("cpu", "gpu") else "gpu"

    def _save_provider(self, provider):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_name(self.settings_path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps({"provider": provider}) + "\n", encoding="utf-8")
            os.replace(temporary, self.settings_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _runtime_status_locked(self):
        state = self.model.get("state", "idle")
        return {"requestedProvider": self.requested_provider,
                "modelProvider": self.model.get("provider") if state == "ready" and
                self.model.get("provider") in ("cpu", "webgpu") else None,
                "status": state if state in ("ready", "loading", "idle", "missing") else "error"}

    def runtime_status(self):
        with self.condition:
            return self._runtime_status_locked()

    def local_model_status(self):
        return self.local_model_source.status()

    def _launch_locked(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.model = {"state": "loading"}
        self.pending.clear()
        self.stream_callbacks.clear()
        self.expired_request_ids.clear()
        command = ["node", "--import", "tsx", str(ROOT / "bridge/analysis_server.ts")]
        command += ["--api-only"] if self.api_only else ["--provider", self.requested_provider]
        environment = os.environ.copy()
        environment["LAYA_MODEL_DIR"] = self.local_model_source.status()["path"]
        self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                                        env=environment,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader_thread = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.reader_thread.start()

    def configure_local_model(self, value):
        if self.api_only:
            raise ValueError("API worker has no local model")
        selected = self.local_model_source.select(value)
        self.analysis_version()
        with self.condition:
            if self.process is None or self.process.poll() is not None:
                self._launch_locked()
                deadline = time.monotonic() + 120
                while self.model["state"] == "loading" and time.monotonic() < deadline:
                    self.condition.wait(timeout=1)
                return {**selected, "model": self._runtime_status_locked()}
            self.model = {"state": "loading"}
            self.serial += 1
            request_id = self.serial
            self.process.stdin.write(json.dumps({"id": request_id, "cmd": "configure-model-dir",
                                                 "modelDir": selected["path"]}) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + 180
            while request_id not in self.pending and time.monotonic() < deadline and self.process.poll() is None:
                self.condition.wait(timeout=1)
            response = self.pending.pop(request_id, None)
            if not response or response.get("analysisVersion") != self.version:
                self.model = {"state": "error", "message": "model switch timed out or version changed"}
            elif response.get("error"):
                self.model = {"state": "error", "message": str(response["error"])[:200]}
            else:
                self.model = response.get("modelStatus") or {"state": "error", "message": "model status missing"}
            return {**selected, "model": self._runtime_status_locked()}

    def configure_runtime(self, provider):
        if self.api_only:
            raise ValueError("API connector has no local runtime provider")
        if provider not in ("cpu", "gpu"):
            raise ValueError("invalid provider")
        # _request holds this same lock until its target reply arrives; switching cannot
        # interrupt an inference or let two JSONL commands write concurrently.
        with self.condition:
            if (provider == self.requested_provider and self.model.get("state") == "ready" and
                    self.model.get("provider") == ("cpu" if provider == "cpu" else "webgpu")):
                return self._runtime_status_locked()
            self._save_provider(provider)
            self.requested_provider = provider
            if self.process is None or self.process.poll() is not None:
                self.model = {"state": "idle"}
                return self._runtime_status_locked()
            self.model = {"state": "loading"}
            self.serial += 1
            request_id = self.serial
            self.process.stdin.write(json.dumps({"id": request_id, "cmd": "configure-runtime",
                                                 "provider": provider}) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + 180
            while request_id not in self.pending and time.monotonic() < deadline and self.process.poll() is None:
                self.condition.wait(timeout=1)
            response = self.pending.pop(request_id, None)
            if not response or response.get("analysisVersion") != self.version:
                self.model = {"state": "error", "message": "runtime switch timed out or version changed"}
            elif response.get("error"):
                self.model = {"state": "error", "message": str(response["error"])[:200]}
            else:
                self.model = response.get("modelStatus") or {"state": "error", "message": "model status missing"}
            return self._runtime_status_locked()

    def analysis_version(self):
        with self.condition:
            if self.version is None:
                probe = subprocess.run(
                    ["node", "--import", "tsx", str(ROOT / "bridge/analysis_server.ts"), "--analysis-version"],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20,
                    check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if probe.returncode != 0:
                    raise RuntimeError("analysis version unavailable")
                try:
                    version = json.loads(probe.stdout.strip()).get("analysisVersion")
                except ValueError as exc:
                    raise RuntimeError("invalid analysis version response") from exc
                if not isinstance(version, str) or not version:
                    raise RuntimeError("invalid analysis version")
                self.version = version
            return self.version

    def _read(self, process):
        for line in process.stdout:
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            stream_callback = None
            stream_delta = reply.get("streamDelta")
            with self.condition:
                if self.process is not process:
                    continue
                if "ready" in reply:
                    self.model = reply.get("model") or {"state": "error", "message": "model status missing"}
                    self.running_version = reply.get("analysisVersion")
                    if self.running_version != self.version:
                        self.model = {"state": "error", "message": "analysis version changed; restart service"}
                elif reply.get("id") is not None and "streamDelta" in reply:
                    # A stream delta is progress only.  The final reply with the
                    # same id is still required before _request() can complete.
                    candidate = self.stream_callbacks.get(reply["id"])
                    if callable(candidate) and isinstance(stream_delta, str) and stream_delta:
                        stream_callback = candidate
                elif reply.get("id") is not None:
                    status = reply.get("modelStatus")
                    # API-only workers have no Laya runtime. An API error used to
                    # carry Laya's default 'loading' status and poison this channel,
                    # making the NEXT request wait 120s before sending anything.
                    if (not self.api_only and isinstance(status, dict) and
                            status.get("state") in ("ready", "loading", "missing", "error")):
                        self.model = status
                    if reply["id"] in self.expired_request_ids:
                        self.expired_request_ids.discard(reply["id"])
                    else:
                        self.pending[reply["id"]] = reply
                self.condition.notify_all()
            if stream_callback is not None:
                try:
                    stream_callback(stream_delta)
                except Exception:
                    # Progress reporting must never kill the reader or turn a
                    # valid final provider response into a failed request.
                    pass
        with self.condition:
            if self.process is process:
                self.model = {"state": "error", "message": "analysis process exited"}
                self.condition.notify_all()

    def _request(self, payload, *, require_model=True, on_stream_delta=None):
        version = self.analysis_version()
        with self.condition:
            self._launch_locked()
            process = self.process
            probe = self.api_only and payload.get("cmd") in ("model:list", "model:test")
            deadline = time.monotonic() + (12 if probe else 120)
            while (self.model["state"] == "loading" and time.monotonic() < deadline and
                   process.poll() is None and self.process is process):
                self.condition.wait(timeout=1)
            if self.model["state"] == "loading":
                raise RuntimeError("timeout" if self.api_only else "model worker startup timeout")
            if self.api_only and self.process is not process:
                raise RuntimeError("model-source-changed")
            if require_model and self.model["state"] in ("missing", "error") and process.poll() is None:
                self.serial += 1
                prepare_id = self.serial
                process.stdin.write(json.dumps({"id": prepare_id, "cmd": "prepare"}) + "\n")
                process.stdin.flush()
                while prepare_id not in self.pending and time.monotonic() < deadline and process.poll() is None:
                    self.condition.wait(timeout=1)
                prepared = self.pending.pop(prepare_id, {})
                self.model = prepared.get("model") or {"state": "error", "message": "model retry failed"}
            if require_model and self.model["state"] != "ready":
                raise RuntimeError("model-" + self.model["state"] + ": " + self.model.get("message", ""))
            if self.running_version != version:
                raise RuntimeError("analysis version changed; restart service")
            self.serial += 1
            request_id = self.serial
            if on_stream_delta is not None:
                self.stream_callbacks[request_id] = on_stream_delta
            try:
                process.stdin.write(json.dumps({"id": request_id, **payload}, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except Exception:
                self.stream_callbacks.pop(request_id, None)
                raise
            response_timeout = (16 if payload.get("cmd") == "model:list" else
                                18 if payload.get("cmd") == "model:test" else 180)
            deadline = time.monotonic() + response_timeout
            while request_id not in self.pending and time.monotonic() < deadline and process.poll() is None:
                self.condition.wait(timeout=1)
            response = self.pending.pop(request_id, None)
            self.stream_callbacks.pop(request_id, None)
            if not response:
                self.expired_request_ids.add(request_id)
                if len(self.expired_request_ids) > 256:
                    self.expired_request_ids.clear()
                raise RuntimeError("timeout" if self.api_only else "analysis process timeout or exited")
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            if response.get("analysisVersion") != version:
                raise RuntimeError("analysis response version mismatch")
            return response, version

    def model_list(self, protocol, base_url, api_key):
        response, _ = self._request({"cmd": "model:list", "protocol": protocol,
                                     "baseUrl": base_url, "apiKey": api_key}, require_model=False)
        return {"models": response.get("models"), "supported": response.get("supported")}

    def model_test(self, protocol, base_url, api_key, model):
        response, _ = self._request({"cmd": "model:test", "protocol": protocol,
                                     "baseUrl": base_url, "apiKey": api_key,
                                     "model": model}, require_model=False)
        return {"ok": response.get("ok"), "latencyMs": response.get("latencyMs")}

    def model_generate(self, protocol, base_url, api_key, model, system, prompt,
                       max_output_tokens=512):
        response, _ = self._request({"cmd": "model:generate", "protocol": protocol,
                                     "baseUrl": base_url, "apiKey": api_key, "model": model,
                                     "system": system, "prompt": prompt,
                                     "maxOutputTokens": max_output_tokens}, require_model=False)
        text = response.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("empty-response")
        return {"text": text, "usage": response.get("usage")}

    def model_insights(self, protocol, base_url, api_key, model, messages, target_ids,
                       on_delta=None):
        response, _ = self._request({"cmd": "model:insights", "protocol": protocol,
                                     "baseUrl": base_url, "apiKey": api_key, "model": model,
                                     "messages": messages, "targetIds": target_ids},
                                    require_model=False, on_stream_delta=on_delta)
        insights = response.get("insights")
        if not isinstance(insights, list) or len(insights) != len(target_ids):
            raise RuntimeError("invalid-insights")
        return {"insights": insights, "usage": response.get("usage"),
                "responseId": response.get("responseId"), "timings": response.get("timings")}

    def model_portrait(self, protocol, base_url, api_key, model, previous, messages):
        response, _ = self._request({"cmd": "model:portrait", "protocol": protocol,
                                     "baseUrl": base_url, "apiKey": api_key, "model": model,
                                     "previous": previous, "messages": messages},
                                    require_model=False)
        portrait = response.get("portrait")
        if not valid_api_portrait(portrait):
            raise RuntimeError("invalid-portrait")
        return portrait

    def refresh_portrait_axes(self, protocol, base_url, api_key, model, portrait):
        response, _ = self._request({"cmd": "model:portrait-axes", "protocol": protocol,
                                     "baseUrl": base_url, "apiKey": api_key, "model": model,
                                     "portrait": portrait},
                                    require_model=False)
        update = response.get("axes")
        if not isinstance(update, dict):
            raise RuntimeError("invalid-portrait")
        return update

    @staticmethod
    def _wire_messages(messages):
        # The legacy four fields stay byte-identical; inputMeta is local-only metadata
        # that the analysis server validates and projects away before the model call.
        wire = []
        for item in messages:
            entry = {"id": item["id"], "side": item["side"], "text": item["text"]}
            if "time" in item:
                entry["time"] = item["time"]
            if "inputMeta" in item:
                entry["inputMeta"] = item["inputMeta"]
            if item.get('conversationKind')=='group':
                from qq_roles import group_context
                entry['groupContext']=group_context(item)
            wire.append(entry)
        return wire

    def analyze(self, session, messages, target, portraitContext=None, messageLabelsOnly=False):
        if portraitContext is not None and not isinstance(portraitContext, str):
            raise ValueError("invalid portraitContext")
        if type(messageLabelsOnly) is not bool:
            raise ValueError("invalid messageLabelsOnly")
        payload = {"cmd": "targets", "sessionId": session,
                   "messages": self._wire_messages(messages), "targetIds": [target]}
        if any(item.get('conversationKind')=='group' for item in messages):
            payload['conversationKind']='group'
        if portraitContext and portraitContext.strip():
            payload["portraitContext"] = portraitContext
            if next((item.get('side') for item in messages if item.get('id')==target),None)=='self':
                payload['portraitContextSide']='self'
        if messageLabelsOnly:
            payload["messageLabelsOnly"] = True
        response, version = self._request(payload)
        if len(response.get("messages", [])) != 1 or response["messages"][0].get("messageId") != target:
            raise RuntimeError("invalid model response")
        return {**response["messages"][0], "analysisVersion": version,
                "_modelMs": response.get("durationMs")}

    def analyze_batch(self, session, messages, context=None):
        """One model judgment for a contiguous new-text window, never per-message calls."""
        payload = [{"id": item["id"], "text": item["text"], "side": item["side"],
                    "target": bool(item.get("target")), "offset": int(item.get("offset", 0))}
                   for item in messages]
        group=any(item.get('conversationKind')=='group' for item in messages)
        if group:
            from qq_roles import group_context
            for item,wire in zip(messages,payload):wire['groupContext']=group_context(item)
        response, version = self._request({"cmd": "batch", "sessionId": session,
                                           "messages": payload, "context": context or [],
                                           **({'conversationKind':'group'} if group else {})})
        if response.get("batchVersion") != "message-batch-v1":
            raise RuntimeError("batch response version mismatch")
        consumed = response.get("consumed")
        if not isinstance(consumed, list) or not 1 <= len(consumed) <= len(payload):
            raise RuntimeError("batch made no progress")
        for index, piece in enumerate(consumed):
            item = payload[index]
            start, end = piece.get("start"), piece.get("end")
            if (piece.get("id") != item["id"] or type(start) is not int or type(end) is not int or
                    start != item["offset"] or not start < end <= len(item["text"]) or
                    type(piece.get("complete")) is not bool or
                    piece["complete"] != (end == len(item["text"])) or
                    (index < len(consumed)-1 and not piece["complete"])):
                raise RuntimeError("invalid batch coverage")
        result = response.get("result")
        targeted = any(payload[index]["target"] and
                       payload[index]["text"][piece["start"]:piece["end"]].replace("\ufeff", "").strip()
                       for index, piece in enumerate(consumed))
        if targeted and not isinstance(result, dict):
            raise RuntimeError("missing batch result")
        if result is not None:
            for field in ("emotion", "intent"):
                values = result.get(field)
                if not isinstance(values, list) or not values or any(
                        not isinstance(item, dict) or not isinstance(item.get("label"), str) or
                        not isinstance(item.get("probability"), (float, int)) or
                        not math.isfinite(item["probability"]) or not 0 <= item["probability"] <= 1
                        for item in values):
                    raise RuntimeError("invalid batch " + field)
            validate_style_evidence(result.get("styleEvidence"))
            validate_personality_evidence(result.get("personalityEvidence"))
            requires_relationship = not group and any(payload[index]['target'] and payload[index]['side']=='other' for index in range(len(consumed)))
            if targeted and ((requires_relationship and result.get('score') is None) or
                             result.get('score') is not None and (not isinstance(result['score'], (float,int)) or
                             not math.isfinite(result['score']) or not -1 <= result['score'] <= 1)):
                raise RuntimeError("invalid batch relationship")
        return {**response, "analysisVersion": version}

    def predict_reply(self, session, messages, draft):
        response, version = self._request({"cmd": "forecast", "sessionId": session,
                                           "messages": self._wire_messages(messages), "draft": draft})
        candidates = response.get("candidates")
        expected = {"small_talk", "share_news", "ask_question", "seek_comfort", "give_comfort",
                    "make_plan", "flirt", "complain", "apologize", "joke", "reject", "distance"}
        if not isinstance(candidates, list) or len(candidates) != 3 or len({
                item.get("id") for item in candidates if isinstance(item, dict)}) != 3:
            raise RuntimeError("invalid reply forecast result")
        for item in candidates:
            if (not isinstance(item, dict) or item.get("id") not in expected or
                    not isinstance(item.get("label"), str) or not item["label"] or
                    not isinstance(item.get("description"), str) or not item["description"] or
                    not isinstance(item.get("probability"), (float, int)) or
                    not math.isfinite(item["probability"]) or
                    not 0 <= item["probability"] <= 1):
                raise RuntimeError("invalid reply forecast candidate")
        if any(candidates[index]["probability"] < candidates[index + 1]["probability"] for index in range(2)):
            raise RuntimeError("reply forecast candidates are not ranked")
        return {"analysisVersion": version, "candidates": candidates}

    def close(self):
        """End only this bridge's model child after in-flight requests have returned."""
        with self.condition:
            process = self.process
            reader_thread = self.reader_thread
            self.process = None
            self.reader_thread = None
            self.model = {"state": "idle"}
            if process is not None and process.poll() is None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
        if process is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            if reader_thread is not None:
                reader_thread.join(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

    def cancel(self):
        """Stop this API-only worker promptly when its model source changes."""
        if not self.api_only:
            return
        with self.condition:
            process = self.process
            reader_thread = self.reader_thread
            self.process = None
            self.reader_thread = None
            self.model = {"state": "idle"}
            self.pending.clear()
            self.condition.notify_all()
        if process is not None and process.poll() is None:
            process.terminate()
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if reader_thread is not None:
                reader_thread.join(timeout=2)
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass

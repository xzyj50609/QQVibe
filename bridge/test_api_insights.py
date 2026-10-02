"""API insight isolation checks with synthetic messages and a fake provider."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from model_source import LOCAL_SOURCE_ID, ModelSourceStore
from model_source import ModelSourceUnavailable
from real_backend import (Backend, ResultStore, WeChatSource, api_portrait_scope, api_portrait_wire_chars,
                          api_portrait_resume_anchor, api_portrait_tail_hashes,
                          empty_api_portrait, empty_profile_state, valid_api_portrait)


class Source:
    def __init__(self, workdir):
        self.account = "account-a"
        self.workdir = str(workdir)
        self.db = object()
        self.history_pages = 0
        self.profile_metadata_calls = 0
        self.profile_invalidations = 0
        self.rows = [
            {"id": "s1", "side": "self", "kind": "text", "text": "周六看展吗？",
             "senderId": "me", "_sort": [1, "shard", 1]},
            {"id": "o1", "side": "other", "kind": "text", "text": "可以呀，我来找你。",
             "senderId": "friend", "_sort": [2, "shard", 2]},
            {"id": "o2", "side": "other", "kind": "text", "text": "我们下午见。",
             "senderId": "friend", "_sort": [3, "shard", 3]},
        ]

    def identity(self):
        return self.account, self.workdir

    def contact(self, user):
        return {"name": user, "avatar": "", "avatarCandidates": []}

    def invalidate_profile_metadata(self, user=None):
        self.profile_invalidations += 1

    def profile_metadata(self, user, member=None):
        self.profile_metadata_calls += 1
        members = [{"id": sender, "name": sender, "avatar": "", "avatarCandidates": []}
                   for sender in sorted({row["senderId"] for row in self.rows
                                         if row["side"] == "other"})]
        if member and (not user.endswith("@chatroom") or
                       member not in {entry["id"] for entry in members}):
            raise ValueError("unknown member")
        selected = member or user
        relevant = [row for row in self.rows if not member or row["senderId"] == member]
        return {"contact": {"name": selected, "avatar": "", "avatarCandidates": []},
                "members": members if user.endswith("@chatroom") else [],
                "count": len(relevant), "textCount": sum(row["kind"] == "text" for row in relevant)}

    def profile_overview(self, user, member=None, highwater=None):
        members = [{"id": sender, "name": sender, "avatar": "", "avatarCandidates": []}
                   for sender in sorted({row["senderId"] for row in self.rows
                                         if row["side"] == "other"})]
        if member and (not user.endswith("@chatroom") or
                       member not in {entry["id"] for entry in members}):
            raise ValueError("unknown member")
        relevant = [row for row in self.rows if not member or row["senderId"] == member]
        selected = member or user
        return {"contact": {"name": selected, "avatar": "", "avatarCandidates": []},
                "members": members if user.endswith("@chatroom") else [],
                "count": len(relevant), "textCount": None}

    def messages(self, user, limit):
        return self.rows[-limit:]

    def history_highwater(self, _user):
        return tuple(self.rows[-1]["_sort"]) if self.rows else None

    def history_page(self, _user, highwater, after=None, page_size=256):
        self.history_pages += 1
        rows = [item for item in self.rows if tuple(item["_sort"]) <= highwater and
                (after is None or tuple(item["_sort"]) > after)][:page_size]
        return rows, tuple(rows[-1]["_sort"]) if rows else None


class Analyzer:
    def __init__(self):
        self.calls = []
        self.insight_payloads = []
        self.portrait_calls = []
        self.axes_calls = 0
        self.fail_portrait_once = False
        self.fail_portrait_at = None
        self.fail_portrait_invalid_once = False
        self.entered = None
        self.release = None
        self.test_entered = None
        self.test_release = None

    def model_test(self, protocol, base_url, api_key, model):
        if self.test_entered:
            self.test_entered.set()
            self.test_release.wait(timeout=3)
        return {"ok": True, "latencyMs": 7}

    def model_insights(self, protocol, base_url, api_key, model, messages, target_ids):
        self.calls.append((protocol, base_url, model, tuple(target_ids)))
        self.insight_payloads.append(messages)
        if self.entered:
            self.entered.set()
            self.release.wait(timeout=3)
        return {"insights": [
            {"id": target_id, "status": "ok", "emotion": "期待", "intent": "邀约"}
            for target_id in target_ids]}

    def model_portrait(self, protocol, base_url, api_key, model, previous, messages):
        self.portrait_calls.append((model, previous, messages))
        if self.fail_portrait_invalid_once:
            self.fail_portrait_invalid_once = False
            raise RuntimeError("invalid-output")
        if self.fail_portrait_once or len(self.portrait_calls) == self.fail_portrait_at:
            self.fail_portrait_once = False
            self.fail_portrait_at = None
            raise RuntimeError("synthetic failure")
        return {"summary": "已观察历史消息", "communication": "交流简洁",
                "emotionExpression": "", "interactionPreferences": "",
                "topics": ["见面"], "patterns": [], "boundaries": [], "uncertain": [],
                "affinity": None, "mbtiAxes": {key: None for key in ("EI", "SN", "TF", "JP")},
                "traits": {key: None for key in ("socialEnergy", "humor", "composure",
                                                     "initiative", "care", "affection")}}

    def refresh_portrait_axes(self, protocol, base_url, api_key, model, previous):
        self.axes_calls += 1
        return {"mbtiAxes": {"EI": 62, "SN": 41, "TF": 55, "JP": 68},
                "traits": {key: 50 for key in ("socialEnergy", "humor", "composure",
                                               "initiative", "care", "affection")},
                "affinity": 60}


class ApiInsightTests(unittest.TestCase):
    def test_legacy_portrait_column_upgrade_survives_concurrent_first_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE api_portrait_v1 (account TEXT NOT NULL, "
                             "session TEXT NOT NULL, source_id TEXT NOT NULL, subject TEXT NOT NULL, "
                             "highwater_json TEXT, after_json TEXT, fingerprint TEXT NOT NULL, "
                             "available_json TEXT NOT NULL, plan_json TEXT NOT NULL, "
                             "batch_index INTEGER NOT NULL, complete INTEGER NOT NULL, "
                             "processed INTEGER NOT NULL, processed_chars INTEGER NOT NULL, "
                             "portrait_json TEXT NOT NULL, "
                             "PRIMARY KEY(account,session,source_id,subject))")
                conn.commit()
            release = threading.Event()
            errors = []
            threads = []
            def first_open():
                release.wait(timeout=2)
                try:
                    ResultStore(path)
                except Exception as exc:
                    errors.append(type(exc).__name__)
            for _ in range(4):
                thread = threading.Thread(target=first_open, daemon=True)
                thread.start()
                threads.append(thread)
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            with closing(sqlite3.connect(path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(api_portrait_v1)")}
            self.assertIn("resume_json", columns)

    def test_api_portrait_scores_allow_unknown_but_reject_invalid_numbers(self):
        portrait = empty_api_portrait()
        self.assertTrue(valid_api_portrait(portrait))
        portrait["affinity"] = 72
        portrait["mbtiAxes"]["EI"] = 35
        portrait["traits"]["initiative"] = 80
        self.assertTrue(valid_api_portrait(portrait))
        portrait["traits"]["initiative"] = 101
        self.assertFalse(valid_api_portrait(portrait))
        portrait["traits"]["initiative"] = True
        self.assertFalse(valid_api_portrait(portrait))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = Source(root)
        self.analyzer = Analyzer()
        self.store = ModelSourceStore(
            root / ".local" / "model-source.json", root=root,
            protect=lambda key: b"wrapped:" + key.encode(),
            unprotect=lambda data: data.removeprefix(b"wrapped:").decode(),
        )
        self.backend = Backend(
            self.source, analyzer=self.analyzer, model_source_store=self.store,
            store_factory=lambda account, _workdir: ResultStore(root / f"{account}.sqlite3"),
        )
        self.addCleanup(self.backend.shutdown)

    def activate(self, model="test-model", key="test-key", context_tokens=8192):
        return self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": model, "apiKey": key, "contextTokens": context_tokens,
        })

    def pin_synthetic_source_root(self):
        """Keep account/workdir unchanged while replacing the underlying source root."""
        current = {"root": "source-root-a"}
        local = threading.local()
        identity = self.source.identity

        @contextmanager
        def request_scope():
            previous = getattr(local, "pinned", None)
            if previous is None:
                local.pinned = current["root"]
            try:
                yield
            finally:
                local.pinned = previous

        def verified_identity():
            pinned = getattr(local, "pinned", None)
            if pinned is not None and pinned != current["root"]:
                raise RuntimeError("source root changed")
            return identity()

        self.source.request_scope = request_scope
        self.source.identity = verified_identity
        return current

    def wait_done(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.backend.model_insights("friend")
            if result["job"]["status"] in ("done", "error"):
                return result
            time.sleep(.01)
        self.fail("API insight job did not finish")

    def wait_portrait(self):
        # Keep the same deadline without repeatedly opening the result DB while
        # its background writer is trying to commit. The real coordinator owns
        # inventory, queued work and retries, so idle includes their completion.
        self.assertTrue(self.backend.api_tasks.wait_for_idle(3), "API portrait job did not finish")
        result = self.backend.model_portrait("friend")
        self.assertIn(result["job"]["status"], ("done", "error"))
        return result

    def wait_portrait_inventory(self, user="friend"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.backend.model_portrait(user)
            if result["inventoryReady"]:
                return result
            self.assertNotEqual(result["inventoryStatus"], "error")
            time.sleep(.01)
        self.fail("API portrait inventory did not finish")

    def test_cached_insights_are_scoped_to_account_and_source(self):
        first = self.activate()
        self.assertEqual(first["mode"], "api")
        self.backend.start_model_insights("account-a", "friend", 2)
        done = self.wait_done()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(set(done["results"]), {"o1", "o2"})
        self.assertEqual(done["results"]["o1"],
                         {"id": "o1", "status": "ok", "affect": {"feeling": "期待"}, "intents": ["邀约"]})
        self.assertEqual(len(self.analyzer.calls), 1)
        self.backend.model_source_activate({"mode": "local"})
        self.assertEqual(self.backend.model_insights("friend")["results"], {})
        second = self.activate()
        self.assertEqual(second["sourceId"], first["sourceId"])
        self.assertEqual(set(self.backend.model_insights("friend")["results"]), {"o1", "o2"})
        self.backend.start_model_insights("account-a", "friend", 2)
        self.assertEqual(len(self.analyzer.calls), 1, "same source should reuse saved results")
        third = self.activate(model="different-model")
        self.assertNotEqual(third["sourceId"], first["sourceId"])
        self.assertEqual(self.backend.model_insights("friend")["results"], {})
        self.source.account = "account-b"
        self.assertEqual(self.backend.model_insights("friend")["results"], {})

    def test_insight_job_records_application_timing_segments_without_chat_text(self):
        self.activate()
        self.backend.start_model_insights("account-a", "friend", 2)
        done = self.wait_done()
        self.assertEqual(done["job"]["status"], "done")
        timings = done["job"]["timings"]
        for field in ("prepareMs", "queueMs", "providerMs", "validateMs", "saveMs", "totalMs"):
            with self.subTest(field=field):
                self.assertIsInstance(timings[field], (int, float))
                self.assertFalse(isinstance(timings[field], bool))
                self.assertGreaterEqual(timings[field], 0)
        self.assertIsNone(timings["firstBodyMs"],
                          "first body is unobservable with buffered connectors and never fabricated")
        self.assertGreaterEqual(timings["totalMs"], timings["prepareMs"])
        self.assertGreaterEqual(timings["totalMs"], timings["providerMs"])
        encoded = json.dumps(timings)
        self.assertNotIn(self.source.rows[1]["text"], encoded)
        self.assertNotIn("test-key", encoded)

    def test_streaming_labels_are_visible_in_job_before_final_save(self):
        self.activate()
        original = self.analyzer.model_insights
        delta_seen = threading.Event()
        release = threading.Event()

        def streaming(protocol, base_url, api_key, model, messages, target_ids, on_delta=None):
            if on_delta:
                on_delta("姓名：朋友\n情感：关切\n意图：询问\n")
            delta_seen.set()
            release.wait(timeout=3)
            return original(protocol, base_url, api_key, model, messages, target_ids)

        self.analyzer.model_insights = streaming
        self.backend.start_model_insights("account-a", "friend", 1)
        self.assertTrue(delta_seen.wait(timeout=2))
        running = self.backend.model_insights("friend")["job"]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["targetIds"], ["o2"])
        self.assertIn("情感：关切", running["partialText"])
        self.assertIsInstance(running["timings"]["firstBodyMs"], (int, float))
        release.set()
        done = self.wait_done()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(done["results"]["o2"]["status"], "ok")

    def test_slow_provider_is_reflected_in_provider_timing(self):
        self.activate()
        original = self.analyzer.model_insights

        def slow(protocol, base_url, api_key, model, messages, target_ids):
            time.sleep(.15)
            return original(protocol, base_url, api_key, model, messages, target_ids)

        self.analyzer.model_insights = slow
        self.backend.start_model_insights("account-a", "friend", 1)
        done = self.wait_done()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(set(done["results"]), {"o2"})
        self.assertGreaterEqual(done["job"]["timings"]["providerMs"], 100)

    def test_same_session_repeat_returns_the_running_job_unchanged(self):
        self.activate()
        self.analyzer.entered = threading.Event()
        self.analyzer.release = threading.Event()
        self.backend.start_model_insights("account-a", "friend", 2)
        self.assertTrue(self.analyzer.entered.wait(timeout=2))
        running = self.backend.model_insights("friend")["job"]
        self.assertEqual(running["status"], "running")
        repeat = self.backend.start_model_insights("account-a", "friend", 2)["job"]
        self.assertEqual(repeat["id"], running["id"])
        self.assertEqual(repeat["status"], "running")
        self.assertEqual(len(self.analyzer.calls), 1)
        self.analyzer.release.set()
        self.wait_done()

    def test_recent_other_is_not_skipped_by_newer_self_messages(self):
        self.source.rows.append({"id": "s2", "side": "self", "kind": "text",
                                 "text": "我到了。", "senderId": "me", "_sort": [4, "shard", 4]})
        self.activate()
        self.backend.start_model_insights("account-a", "friend", 1)
        done = self.wait_done()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(set(done["results"]), {"o2"})
        self.assertEqual(self.analyzer.calls[0][3], ("o2",))

    def test_client_targets_are_resolved_against_the_current_conversation(self):
        self.activate()
        with self.assertRaises(ValueError):
            self.backend.start_model_insights("account-a", "friend", 501)
        self.assertEqual(self.analyzer.calls, [])
        self.backend.start_model_insights("account-a", "friend", 1, ["o1"])
        done = self.wait_done()
        self.assertEqual(set(done["results"]), {"o1"})
        self.assertEqual(self.analyzer.calls[0][3], ("o1",))
        with self.assertRaises(ValueError):
            self.backend.start_model_insights("account-a", "friend", 1, ["s1"])
        with self.assertRaises(ValueError):
            self.backend.start_model_insights("account-a", "friend", 1, ["other-chat-id"])

    def test_history_targets_are_resolved_from_the_selected_page(self):
        self.activate()
        older = {"id": "old", "side": "other", "kind": "text", "text": "上周见面吗？",
                 "senderId": "friend", "_sort": [0, "shard", 0]}
        with patch("backend_service.browse_history", return_value={"messages": [older, *self.source.rows]}) as browse:
            self.backend.start_model_insights("account-a", "friend", 1, ["old"], "history-anchor")
            done = self.wait_done()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(self.analyzer.calls[-1][3], ("old",))
        self.assertIn("old", self.backend.model_insights("friend", ids=["old"])["results"])
        browse.assert_called_once()
        self.assertEqual(browse.call_args.kwargs["limit"], 500)
        with patch("backend_service.browse_history", return_value={"messages": self.source.rows}):
            with self.assertRaises(ValueError):
                self.backend.start_model_insights("account-a", "friend", 1, ["outside"], "history-anchor")

    def test_source_switch_does_not_block_polling_or_save_stale_results(self):
        first = self.activate()
        self.analyzer.entered = threading.Event()
        self.analyzer.release = threading.Event()
        self.backend.start_model_insights("account-a", "friend", 2)
        self.assertTrue(self.analyzer.entered.wait(timeout=2))
        started = time.monotonic()
        running = self.backend.model_insights("friend")
        self.assertEqual(running["job"]["status"], "running")
        self.assertLess(time.monotonic() - started, .5)
        switched = []
        thread = threading.Thread(target=lambda: switched.append(
            self.backend.model_source_activate({"mode": "local"})))
        thread.start()
        thread.join(timeout=.5)
        self.assertFalse(thread.is_alive(), "source switching should not wait for the provider")
        self.assertEqual(switched[0]["mode"], "local")
        self.analyzer.release.set()
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.model_insights("friend")["results"], {})
        self.backend.model_source_activate({"mode": "api", "protocol": "responses",
                                            "baseUrl": "https://example.test/v1",
                                            "model": "test-model", "apiKey": "test-key",
                                            "contextTokens": 8192})
        self.assertEqual(self.backend.active_model_source_id, first["sourceId"])
        self.assertEqual(self.backend.model_insights("friend")["results"], {})

    def test_current_account_clear_drains_api_worker(self):
        self.activate()
        self.analyzer.entered = threading.Event()
        self.analyzer.release = threading.Event()
        self.backend.start_model_insights("account-a", "friend", 2)
        self.assertTrue(self.analyzer.entered.wait(timeout=2))
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (self.backend.pause_for_account_clear("account-a"),
                                                  finished.set()))
        thread.start()
        self.assertFalse(finished.wait(timeout=.1))
        self.analyzer.release.set()
        self.assertTrue(finished.wait(timeout=3))
        thread.join(timeout=1)

    def test_late_api_activation_cannot_override_a_new_local_selection(self):
        self.analyzer.test_entered = threading.Event()
        self.analyzer.test_release = threading.Event()
        errors = []
        thread = threading.Thread(target=lambda: self._activate_later(errors))
        thread.start()
        self.assertTrue(self.analyzer.test_entered.wait(timeout=2))
        self.assertEqual(self.backend.model_source_activate({"mode": "local"})["mode"], "local")
        self.analyzer.test_release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ModelSourceUnavailable)
        self.assertEqual(self.backend.model_source()["mode"], "local")

    def test_full_history_portrait_is_batched_resumable_and_source_scoped(self):
        for index in range(4, 38):
            self.source.rows.append({"id": f"o{index}", "side": "other", "kind": "text",
                                     "text": f"第{index}条历史消息", "senderId": "friend",
                                     "_sort": [index, "shard", index]})
        first = self.activate()
        self.analyzer.fail_portrait_once = True
        self.backend.start_model_portrait("account-a", "friend")
        failed = self.wait_portrait()
        self.assertEqual(failed["job"]["status"], "error")
        self.assertFalse(failed["progress"]["complete"])
        self.assertEqual(failed["progress"]["processed"], 0)
        self.backend.start_model_portrait("account-a", "friend")
        done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done")
        self.assertTrue(done["progress"]["complete"])
        self.assertEqual(done["progress"]["processed"], 37)
        target_count = sum(item["side"] == "other" and item["kind"] == "text"
                           for item in self.source.rows)
        self.assertEqual(done["progress"]["processedTargetTexts"], target_count)
        self.assertEqual(done["progress"]["totalTargetTexts"], target_count)
        self.assertLess(len(self.analyzer.portrait_calls), 36)
        self.assertEqual({row["id"].split(":")[0] for _, _, batch in self.analyzer.portrait_calls[1:]
                          for row in batch}, {item["id"] for item in self.source.rows})
        self.assertTrue(all(not row["target"] for _, _, batch in self.analyzer.portrait_calls[1:]
                            for row in batch if row["sender"] == "SELF"))
        self.source.rows.append({"id": "new", "side": "other", "kind": "text", "text": "新消息",
                                 "senderId": "friend", "_sort": [38, "shard", 38]})
        pages_before_update = self.source.history_pages
        self.backend.start_model_portrait("account-a", "friend")
        updated = self.wait_portrait()
        self.assertEqual(updated["progress"]["processed"], 38)
        self.assertEqual(updated["progress"]["processedTargetTexts"], target_count + 1)
        self.assertEqual(self.analyzer.portrait_calls[-1][2][0]["id"], "new:0")
        self.assertEqual(self.source.history_pages, pages_before_update + 2,
                         "new-message portrait should take one full history traversal")
        self.backend.start_model_insights("account-a", "friend", 1)
        self.wait_done()
        self.assertEqual(self.analyzer.insight_payloads[-1][-1]["portraitContext"],
                         "已观察历史消息")
        second = self.activate(model="other-model")
        self.assertNotEqual(second["sourceId"], first["sourceId"])
        self.backend.start_model_insights("account-a", "friend", 1)
        self.wait_done()
        self.assertNotIn("portraitContext", self.analyzer.insight_payloads[-1][-1])

    def test_ten_thousand_message_resume_reads_only_unfinished_suffix(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "other", "kind": "text", "text": "合成消息",
             "senderId": "friend", "_sort": [index, "shard", index]}
            for index in range(1, 10001)
        ]
        selected = self.activate(context_tokens=1000000)
        account, workdir, store = self.backend._scoped_identity()
        scope = (account, workdir)
        highwater = self.source.history_highwater("friend")
        pieces, available, fingerprint, full, full_fingerprint, rows = (
            self.backend._api_portrait_history("friend", "friend", highwater, scope))
        self.assertEqual((len(pieces), full["textCount"]), (10000, 10000))
        available.update(baseTextCount=0, baseTargetTextCount=0,
                         fullAvailable=full, fullFingerprint=full_fingerprint,
                         processedTargetTextCount=8664)
        source_scope = api_portrait_scope(selected["sourceId"])
        store.api_portrait_begin(account, "friend", source_scope, "friend", highwater,
                                 None, fingerprint, available, [8664, 10000])
        store.api_portrait_checkpoint(
            account, "friend", source_scope, "friend", 1, empty_api_portrait(), 8664,
            sum(len(piece["text"]) for piece in pieces[:8664]), False,
            processed_target=8664,
            resume=api_portrait_resume_anchor(
                pieces[8663], 8664, 1, api_portrait_tail_hashes(rows)))

        page_rows = []
        original_page = self.source.history_page
        def counted_page(*args, **kwargs):
            page, cursor = original_page(*args, **kwargs)
            page_rows.append(len(page))
            return page, cursor
        self.source.history_page = counted_page
        entered = threading.Event()
        def stop_before_model(*_args):
            entered.set()
            raise RuntimeError("synthetic stop before paid model call")
        self.analyzer.model_portrait = stop_before_model
        started_at = time.monotonic()
        started = self.backend.start_model_portrait(account, "friend")
        self.assertLess(time.monotonic() - started_at, .5)
        self.assertTrue(entered.wait(timeout=3), "suffix preparation should reach the model")
        preparation_seconds = time.monotonic() - started_at
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertEqual(sum(page_rows), 1336,
                         "resuming at 8664/10000 must not read the completed 8664 rows")
        self.assertLessEqual(len(page_rows), 3)
        self.assertLess(preparation_seconds, 3,
                        "synthetic suffix preparation should avoid a full-history delay")
        saved = store.api_portrait_get(account, "friend", source_scope, "friend")
        self.assertEqual((saved["batchIndex"], saved["processed"]), (1, 8664),
                         "a failed next model call must retain the committed checkpoint")
        self.assertEqual(started["job"].get("phase"), "preparing")

    def test_resume_inside_long_message_skips_only_committed_pieces(self):
        self.source.rows = [
            {"id": "before", "side": "self", "kind": "text", "text": "前文",
             "senderId": "me", "_sort": [1, "shard", 1]},
            {"id": "long", "side": "other", "kind": "text", "text": "测" * 2500,
             "senderId": "friend", "_sort": [2, "shard", 2]},
            {"id": "after", "side": "other", "kind": "text", "text": "回复",
             "senderId": "friend", "_sort": [3, "shard", 3]},
        ]
        selected = self.activate(context_tokens=4096)
        account, workdir, store = self.backend._scoped_identity()
        highwater = self.source.history_highwater("friend")
        pieces, available, fingerprint, full, full_fingerprint, rows = (
            self.backend._api_portrait_history("friend", "friend", highwater,
                                               (account, workdir)))
        self.assertEqual([piece["id"] for piece in pieces],
                         ["before:0", "long:0", "long:1", "long:2", "after:0"])
        available.update(baseTextCount=0, baseTargetTextCount=0,
                         fullAvailable=full, fullFingerprint=full_fingerprint,
                         processedTargetTextCount=0)
        source_scope = api_portrait_scope(selected["sourceId"])
        store.api_portrait_begin(account, "friend", source_scope, "friend", highwater,
                                 None, fingerprint, available, [2, 5])
        store.api_portrait_checkpoint(
            account, "friend", source_scope, "friend", 1, empty_api_portrait(), 1,
            len(pieces[0]["text"]) + len(pieces[1]["text"]), False,
            processed_target=0,
            resume=api_portrait_resume_anchor(pieces[1], 2, 1,
                                              api_portrait_tail_hashes(rows)))
        self.backend.start_model_portrait(account, "friend")
        done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done")
        sent = [piece["id"] for _model, _prior, batch in self.analyzer.portrait_calls
                for piece in batch]
        self.assertEqual(sent, ["long:1", "long:2", "after:0"])
        self.assertEqual((done["progress"]["processed"],
                          done["progress"]["processedTargetTexts"]), (3, 2),
                         "a split target message must count once only at its final piece")

    def test_legacy_resume_migrates_once_and_preserves_checkpoint_on_failure(self):
        selected = self.activate()
        account, workdir, store = self.backend._scoped_identity()
        highwater = self.source.history_highwater("friend")
        pieces, available, fingerprint, _full, _full_fingerprint, _rows = (
            self.backend._api_portrait_history("friend", "friend", highwater,
                                               (account, workdir)))
        available.update(baseTextCount=0, baseTargetTextCount=0)
        source_scope = api_portrait_scope(selected["sourceId"])
        store.api_portrait_begin(account, "friend", source_scope, "friend", highwater,
                                 None, fingerprint, available, [1, len(pieces)])
        old_portrait = empty_api_portrait()
        old_portrait["summary"] = "旧摘要"
        store.api_portrait_checkpoint(account, "friend", source_scope, "friend", 1,
                                      old_portrait, 1, len(pieces[0]["text"]), False)
        self.analyzer.fail_portrait_once = True
        pages_before = self.source.history_pages
        self.backend.start_model_portrait(account, "friend")
        failed = self.wait_portrait()
        self.assertEqual(failed["job"]["status"], "error")
        self.assertGreater(self.source.history_pages - pages_before, 0,
                           "legacy row requires one full validation scan")
        migrated = store.api_portrait_get(account, "friend", source_scope, "friend")
        self.assertEqual((migrated["batchIndex"], migrated["processed"],
                          migrated["portrait"]["summary"]), (1, 1, "旧摘要"))
        self.assertIsNotNone(migrated["resume"])
        self.assertEqual(migrated["available"]["fullAvailable"]["textCount"], 3)
        self.assertEqual(migrated["available"]["processedTargetTextCount"], 0)

        page_rows = []
        original_page = self.source.history_page
        def counted_page(*args, **kwargs):
            page, cursor = original_page(*args, **kwargs)
            page_rows.append(len(page))
            return page, cursor
        self.source.history_page = counted_page
        self.backend.start_model_portrait(account, "friend")
        done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(sum(page_rows), 2,
                         "a migrated old checkpoint must seek past its committed prefix")
        self.assertEqual(done["progress"]["processed"], 3)

    def test_new_message_uses_delta_scan_and_saved_full_denominator(self):
        self.activate()
        self.backend.start_model_portrait("account-a", "friend")
        self.assertEqual(self.wait_portrait()["job"]["status"], "done")
        self.source.rows.append({"id": "new", "side": "other", "kind": "text",
                                 "text": "合成新增一条", "senderId": "friend",
                                 "_sort": [4, "shard", 4]})
        page_rows = []
        original_page = self.source.history_page
        def counted_page(*args, **kwargs):
            page, cursor = original_page(*args, **kwargs)
            page_rows.append(len(page))
            return page, cursor
        self.source.history_page = counted_page
        self.backend.start_model_portrait("account-a", "friend")
        done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(sum(page_rows), 1,
                         "one new message must not re-render the completed history")
        self.assertEqual((done["job"]["processed"], done["job"]["total"]), (4, 4))
        self.assertEqual((done["progress"]["processed"], done["progress"]["total"]),
                         (4, 4), "GET must show cumulative, not one-message delta totals")
        self.assertEqual(done["available"]["textCount"], 4)
        self.assertEqual(done["available"]["targetTextCount"], 3)

    def test_zero_batch_resume_rejects_changed_frozen_text_without_model_call(self):
        selected = self.activate()
        account, workdir, store = self.backend._scoped_identity()
        highwater = self.source.history_highwater("friend")
        pieces, available, fingerprint, full, full_fingerprint, _rows = (
            self.backend._api_portrait_history("friend", "friend", highwater,
                                               (account, workdir)))
        available.update(baseTextCount=0, baseTargetTextCount=0,
                         fullAvailable=full, fullFingerprint=full_fingerprint)
        source_scope = api_portrait_scope(selected["sourceId"])
        store.api_portrait_begin(account, "friend", source_scope, "friend", highwater,
                                 None, fingerprint, available, [len(pieces)])
        self.source.rows[0] = {**self.source.rows[0], "text": "同一水位但内容已改"}
        self.backend.start_model_portrait(account, "friend")
        failed = self.wait_portrait()
        self.assertEqual(failed["job"]["status"], "error")
        self.assertEqual(self.analyzer.portrait_calls, [])
        saved = store.api_portrait_get(account, "friend", source_scope, "friend")
        self.assertEqual((saved["fingerprint"], saved["batchIndex"], saved["processed"]),
                         (fingerprint, 0, 0), "source mutation must not advance the frozen cursor")

    def test_portrait_start_returns_before_scan_dedupes_members_and_cancels_on_source_switch(self):
        self.source.rows = [
            {"id": "g1", "side": "other", "kind": "text", "text": "合成消息一",
             "senderId": "friend", "_sort": [1, "shard", 1]},
            {"id": "g2", "side": "other", "kind": "text", "text": "合成消息二",
             "senderId": "other-friend", "_sort": [2, "shard", 2]},
        ]
        first_source = self.activate()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.source.history_page

        def blocked_history_page(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)

        self.source.history_page = blocked_history_page
        try:
            started_at = time.monotonic()
            first = self.backend.start_model_portrait("account-a", "room@chatroom", "friend")
            self.assertLess(time.monotonic() - started_at, .5,
                            "POST must register and return while history is still blocked")
            self.assertTrue(entered.wait(timeout=1), "history preparation should run in a worker")
            self.assertIn(first["job"]["status"], ("queued", "running"))
            self.assertEqual(first["job"].get("phase"), "preparing")
            duplicate = self.backend.start_model_portrait("account-a", "room@chatroom", "friend")
            other_member = self.backend.start_model_portrait(
                "account-a", "room@chatroom", "other-friend")
            self.assertEqual(duplicate["job"]["id"], first["job"]["id"],
                             "same account, conversation, source, and member must dedupe")
            self.assertNotEqual(other_member["job"]["id"], first["job"]["id"],
                                "different group members need separate portrait jobs")
            store = self.backend.store_factory("account-a", self.source.workdir)
            self.assertIsNone(store.api_portrait_get(
                "account-a", "room@chatroom", api_portrait_scope(first_source["sourceId"]), "friend"),
                "preparing a history snapshot must not persist or advance the portrait cursor")

            second_source = self.activate(model="other-model")
            self.assertNotEqual(first_source["sourceId"], second_source["sourceId"])
        finally:
            release.set()
            self.source.history_page = original
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertIsNone(store.api_portrait_get(
            "account-a", "room@chatroom", api_portrait_scope(first_source["sourceId"]), "friend"),
            "a cancelled old-source scan must not write a portrait or cursor")
        self.assertNotIn(("account-a", "room@chatroom", first_source["sourceId"], "friend"),
                         self.backend.api_portrait_jobs)

    def test_portrait_preparation_aborts_when_account_identity_changes(self):
        self.activate()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.source.history_page

        def blocked_history_page(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)

        self.source.history_page = blocked_history_page
        first = self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(entered.wait(timeout=1), "history preparation should run in a worker")
        self.source.account = "account-b"
        release.set()
        self.source.history_page = original
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertIsNone(self.backend.store_factory("account-a", self.source.workdir).api_portrait_get(
            "account-a", "friend", api_portrait_scope(first["sourceId"]), "friend"))
        self.assertIsNone(self.backend.store_factory("account-b", self.source.workdir).api_portrait_get(
            "account-b", "friend", api_portrait_scope(first["sourceId"]), "friend"),
            "a job captured under another account must not write into the new account")
        next_job = self.backend.start_model_portrait("account-b", "friend")
        self.assertEqual(next_job["job"]["status"], "queued")
        result = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.backend.model_portrait("friend")
            if result["job"]["status"] in ("done", "error"):
                break
            time.sleep(.01)
        self.assertEqual(result["job"]["status"], "done")

    def test_portrait_preparation_rejects_changed_root_with_same_account_and_workdir(self):
        selected = self.activate()
        current = self.pin_synthetic_source_root()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.source.history_page

        def blocked_history_page(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)

        self.source.history_page = blocked_history_page
        self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(entered.wait(timeout=1))
        current["root"] = "source-root-b"
        release.set()
        self.source.history_page = original
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertEqual(self.source.account, "account-a")
        self.assertIsNone(self.backend.store_factory("account-a", self.source.workdir).api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend"),
            "an unchanged account/workdir must not allow a changed source root to begin a cursor")
        self.assertEqual(self.analyzer.portrait_calls, [])

    def test_portrait_preparation_stops_on_shutdown_without_persisting_cursor(self):
        selected = self.activate()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.source.history_page

        def blocked_history_page(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)

        self.source.history_page = blocked_history_page
        self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(entered.wait(timeout=1), "history preparation should run in a worker")
        shutdown = threading.Thread(target=lambda: (self.backend.shutdown(), stopped.set()), daemon=True)
        shutdown.start()
        deadline = time.monotonic() + 1
        while not self.backend.closing and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.backend.closing, "shutdown should cancel preparation before draining it")
        release.set()
        self.source.history_page = original
        shutdown.join(timeout=3)
        self.assertFalse(shutdown.is_alive())
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertIsNone(self.backend.store_factory("account-a", self.source.workdir).api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend"),
            "shutdown during history preparation must not commit a cursor")

    def test_returning_to_api_source_reuses_saved_portrait_progress(self):
        first = self.activate(model="model-a", key="synthetic-key-a")
        self.backend.start_model_portrait("account-a", "friend")
        completed = self.wait_portrait()
        self.assertTrue(completed["progress"]["complete"])
        calls = len(self.analyzer.portrait_calls)

        second = self.activate(model="model-b", key="synthetic-key-b")
        self.assertNotEqual(second["sourceId"], first["sourceId"])
        restored = self.activate(model="model-a", key="synthetic-rotated-key-a")
        self.assertEqual(restored["sourceId"], first["sourceId"])
        view = self.backend.model_portrait("friend")
        self.assertEqual(view["progress"], completed["progress"])
        self.assertEqual(view["portrait"], completed["portrait"])
        self.assertEqual(len(self.analyzer.portrait_calls), calls)

    def test_api_persona_metadata_and_generation_do_not_start_local_analysis(self):
        self.activate()
        with (patch.object(self.backend, "start", side_effect=AssertionError("local analysis started")),
              patch.object(self.source, "profile_metadata", side_effect=AssertionError("history metadata scanned"))):
            initial = self.backend.model_portrait("friend")
            self.assertEqual(initial["identity"]["name"], "friend")
            self.assertFalse(initial["identity"]["isGroup"])
            self.backend.start_model_portrait("account-a", "friend")
            done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(done["portrait"]["summary"], "已观察历史消息")
        self.assertEqual(self.backend.jobs, {})
        member = self.backend.model_portrait("room@chatroom", "friend")
        self.assertEqual(member["identity"]["username"], "friend")
        self.assertTrue(member["identity"]["isGroup"])
        self.assertEqual(member["identity"]["members"][0]["id"], "friend")

    def test_group_portrait_read_reuses_inventory_without_rescanning_text(self):
        self.source.rows = [
            {"id": f"o{index}", "side": "other", "kind": "text", "text": f"群消息{index}",
             "senderId": "friend", "_sort": [index, "shard", index]}
            for index in range(1, 5)
        ]
        self.activate()
        rescans_before = self.source.profile_metadata_calls
        result = self.backend.model_portrait("room@chatroom", "friend")
        deadline = time.monotonic() + 3
        while not result["inventoryReady"] and time.monotonic() < deadline:
            result = self.backend.model_portrait("room@chatroom", "friend")
            time.sleep(.01)
        self.assertTrue(result["inventoryReady"])
        self.assertEqual(result["identity"]["messageCount"], 4)
        self.assertEqual(result["identity"]["textCount"], result["available"]["targetTextCount"])
        counted_pages = self.source.history_pages
        for _ in range(3):
            again = self.backend.model_portrait("room@chatroom", "friend")
            self.assertEqual(again["identity"]["textCount"], result["available"]["targetTextCount"])
        self.assertEqual(self.source.history_pages, counted_pages,
                         "persisted inventory must not be rescanned on every poll")
        self.assertEqual(self.source.profile_metadata_calls, rescans_before,
                         "group metadata must not classify every message on every read")

    def test_missing_local_model_profile_does_not_queue_analysis(self):
        class SavedBatch:
            def ensure(self, *_args):
                return {"state": empty_profile_state(), "complete": False}

        self.backend.batch_engine = SavedBatch()
        self.analyzer.analysis_version = lambda: "synthetic-version"
        self.analyzer.local_model_status = lambda: {"state": "missing"}
        with patch.object(self.backend, "start", side_effect=AssertionError("local analysis started")):
            missing = self.backend.profile("friend")
            self.assertEqual(missing["job"]["status"], "missing-model")
            self.activate()
            inactive = self.backend.profile("friend")
            self.assertEqual(inactive["job"]["status"], "inactive-source")
        self.assertEqual(self.backend.jobs, {})

    def test_context_budget_auto_splits_all_text_and_resumes_after_second_batch_failure(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "self" if index % 2 else "other",
             "kind": "text", "text": f"消息{index}" * 4,
             "senderId": "me" if index % 2 else "friend",
             "_sort": [index, "shard", index]}
            for index in range(1, 71)
        ]
        self.activate(context_tokens=4096)
        first_read = self.backend.model_portrait("friend")
        self.assertIsNone(first_read["available"])
        self.assertFalse(first_read["inventoryReady"])
        available = self.wait_portrait_inventory()["available"]
        self.assertEqual((available["textCount"], available["targetTextCount"]), (70, 35))
        counted_pages = self.source.history_pages
        self.backend.model_portrait("friend")
        self.assertEqual(self.source.history_pages, counted_pages,
                         "polling should reuse the inventory for an unchanged highwater")
        self.analyzer.fail_portrait_at = 2
        self.backend.start_model_portrait("account-a", "friend")
        failed = self.wait_portrait()
        first_batch = self.analyzer.portrait_calls[0][2]
        second_batch = self.analyzer.portrait_calls[1][2]
        self.assertEqual((failed["progress"]["processed"], failed["progress"]["batchIndex"],
                          failed["progress"]["complete"]),
                         (len(first_batch), 1, False))
        self.assertGreater(failed["progress"]["batchTotal"], 1)
        self.assertEqual(second_batch[0]["id"], f"m{len(first_batch) + 1}:0")
        self.backend.start_model_portrait("account-a", "friend")
        done = self.wait_portrait()
        self.assertEqual((done["progress"]["processed"], done["progress"]["total"],
                          done["progress"]["complete"]), (70, 70, True))
        self.assertEqual(self.analyzer.portrait_calls[2][2], second_batch,
                         "retry must replay only the uncommitted batch")
        successful = [self.analyzer.portrait_calls[0], *self.analyzer.portrait_calls[2:]]
        self.assertEqual([row["id"] for _, _, batch in successful for row in batch],
                         [f"m{index}:0" for index in range(1, 71)])
        budget = api_portrait_wire_chars(4096)
        self.assertTrue(all(sum(len(json.dumps(
            {key: row[key] for key in ("id", "sender", "target", "text")},
            ensure_ascii=False, separators=(",", ":"))) + 1 for row in batch) <= budget
                            for _, _, batch in successful))
        self.assertEqual(done["progress"]["batchTotal"], len(successful))

    def test_larger_context_uses_fewer_automatic_portrait_batches(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "other", "kind": "text",
             "text": "合成历史消息" * 10, "senderId": "friend",
             "_sort": [index, "shard", index]}
            for index in range(1, 71)
        ]
        self.activate(model="small-context", context_tokens=4096)
        self.backend.start_model_portrait("account-a", "friend")
        small = self.wait_portrait()
        self.activate(model="large-context", context_tokens=16384)
        self.backend.start_model_portrait("account-a", "friend")
        large = self.wait_portrait()
        self.assertEqual(small["progress"]["processed"], 70)
        self.assertEqual(large["progress"]["processed"], 70)
        self.assertGreater(small["progress"]["batchTotal"],
                           large["progress"]["batchTotal"])

    def test_context_change_replans_only_unfinished_portrait_batches(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "other", "kind": "text",
             "text": "合成消息" * 8, "senderId": "friend",
             "_sort": [index, "shard", index]}
            for index in range(1, 71)
        ]
        first = self.activate(context_tokens=4096)
        self.analyzer.fail_portrait_at = 2
        self.backend.start_model_portrait("account-a", "friend")
        failed = self.wait_portrait()
        self.assertEqual(failed["job"]["status"], "error")
        saved = self.backend.store_factory("account-a", self.source.workdir).api_portrait_get(
            "account-a", "friend", api_portrait_scope(first["sourceId"]), "friend")
        old_plan = saved["plan"]
        self.assertEqual(saved["batchIndex"], 1)
        second = self.activate(context_tokens=16384)
        self.assertEqual(second["sourceId"], first["sourceId"])
        self.assertEqual(self.backend.model_portrait("friend")["job"]["status"], "idle")
        self.backend.start_model_portrait("account-a", "friend")
        done = self.wait_portrait()
        self.assertTrue(done["progress"]["complete"])
        self.assertEqual(done["progress"]["processed"], 70)
        self.assertLess(done["progress"]["batchTotal"], len(old_plan))
        self.assertEqual(self.analyzer.portrait_calls[2][2][0]["id"],
                         f"m{old_plan[0] + 1}:0")

    def test_context_change_stops_running_old_budget_before_checkpoint(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "other", "kind": "text",
             "text": "合成消息" * 8, "senderId": "friend",
             "_sort": [index, "shard", index]}
            for index in range(1, 41)
        ]
        first = self.activate(context_tokens=4096)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.analyzer.model_portrait
        def delayed(*args):
            entered.set()
            release.wait(timeout=3)
            return original(*args)
        self.analyzer.model_portrait = delayed
        self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(entered.wait(timeout=1))
        second = self.activate(context_tokens=16384)
        self.assertEqual(second["sourceId"], first["sourceId"])
        release.set()
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        view = self.backend.model_portrait("friend")
        self.assertEqual(view["job"]["status"], "idle")
        self.assertEqual(view["progress"]["processed"], 0)
        self.analyzer.model_portrait = original
        self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(self.wait_portrait()["progress"]["complete"])

    def test_inventory_distinguishes_all_text_from_target_text(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "other" if index == 10 else "self",
             "kind": "text" if index < 11 else "other", "text": f"合成消息{index}",
             "senderId": "friend" if index == 10 else "me",
             "_sort": [index, "shard", index]}
            for index in range(17)
        ]
        self.activate()
        first = self.backend.model_portrait("friend")
        self.assertEqual(first["inventoryStatus"], "running")
        available = self.wait_portrait_inventory()["available"]
        self.assertEqual((available["messageCount"], available["textCount"],
                          available["targetTextCount"]), (17, 11, 1))
        counted_pages = self.source.history_pages
        self.backend.start_model_portrait("account-a", "friend")
        done = self.wait_portrait()
        self.assertEqual(done["progress"]["processed"], 11)
        self.assertEqual(self.source.history_pages, counted_pages,
                         "small inventory should hand off its bounded pieces to POST")
        wire = [item for _, _, batch in self.analyzer.portrait_calls for item in batch]
        self.assertEqual((len(wire), sum(item["target"] for item in wire)), (11, 1))

    def test_saved_portrait_read_does_not_rescan_history(self):
        self.activate()
        self.backend.start_model_portrait("account-a", "friend")
        self.assertEqual(self.wait_portrait()["job"]["status"], "done")
        counted_pages = self.source.history_pages
        self.source.rows.append({"id": "later", "side": "other", "kind": "text",
                                 "text": "合成新增消息", "senderId": "friend",
                                 "_sort": [4, "shard", 4]})
        result = self.backend.model_portrait("friend")
        self.assertEqual(result["portrait"]["summary"], "已观察历史消息")
        self.assertEqual(result["inventoryStatus"], "ready")
        self.assertIsNotNone(result["available"], "saved inventory renders immediately")
        self.assertEqual(self.source.history_pages, counted_pages,
                         "a saved portrait read must not rescan full history")
        self.assertFalse(result["progress"]["complete"],
                         "a newer message still marks the saved portrait out of date")

    def test_portrait_axes_refresh_reuses_saved_portrait_without_history_scan(self):
        self.activate()
        self.backend.start_model_portrait("account-a", "friend")
        done = self.wait_portrait()
        self.assertTrue(done["progress"]["complete"])
        before = self.backend.model_portrait("friend")
        self.assertIsNone(before["portrait"]["mbtiAxes"]["EI"],
                          "the saved portrait starts with unknown axes")
        counted_pages = self.source.history_pages
        batch_calls = len(self.analyzer.portrait_calls)
        started = self.backend.start_model_portrait("account-a", "friend", refresh_axes=True)
        self.assertIn(started["job"]["status"], ("queued", "running"))
        view = self.wait_portrait()
        self.assertEqual(view["job"]["status"], "done")
        self.assertEqual(view["portrait"]["mbtiAxes"]["EI"], 62)
        self.assertEqual(view["progress"]["processed"], before["progress"]["processed"],
                         "axes refresh must preserve progress")
        self.assertEqual(view["progress"]["batchIndex"], before["progress"]["batchIndex"])
        self.assertEqual(self.source.history_pages, counted_pages,
                         "axes refresh must not rescan history")
        self.assertEqual(len(self.analyzer.portrait_calls), batch_calls,
                         "axes refresh must not replay full portrait batches")
        self.assertEqual(self.analyzer.axes_calls, 1)

    def test_axes_refresh_skips_call_after_account_switch_before_worker_starts(self):
        selected = self.activate()
        self.backend.start_model_portrait("account-a", "friend")
        self.assertEqual(self.wait_portrait()["job"]["status"], "done")
        store = self.backend.store_factory("account-a", self.source.workdir)
        before = store.api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.backend._assert_scope

        def blocked_scope(scope):
            entered.set()
            release.wait(timeout=3)
            return original(scope)

        with patch.object(self.backend, "_assert_scope", side_effect=blocked_scope):
            self.backend.start_model_portrait("account-a", "friend", refresh_axes=True)
            self.assertTrue(entered.wait(timeout=1))
            self.source.account = "account-b"
            release.set()
            deadline = time.monotonic() + 3
            while self.backend.api_inflight and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertEqual(self.analyzer.axes_calls, 0,
                         "an old-account axes job must not call the provider")
        after = store.api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        self.assertEqual(after["portrait"], before["portrait"])
        self.assertEqual(after["batchIndex"], before["batchIndex"])

    def test_axes_refresh_discards_result_after_account_switch_during_call(self):
        selected = self.activate()
        self.backend.start_model_portrait("account-a", "friend")
        self.assertEqual(self.wait_portrait()["job"]["status"], "done")
        store = self.backend.store_factory("account-a", self.source.workdir)
        before = store.api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.analyzer.refresh_portrait_axes

        def delayed(*args):
            entered.set()
            release.wait(timeout=3)
            return original(*args)

        self.analyzer.refresh_portrait_axes = delayed
        self.backend.start_model_portrait("account-a", "friend", refresh_axes=True)
        self.assertTrue(entered.wait(timeout=1))
        self.source.account = "account-b"
        release.set()
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        after = store.api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        self.assertEqual(after["portrait"], before["portrait"],
                         "the axes result must not overwrite an old account after identity changes")
        self.assertEqual(after["batchIndex"], before["batchIndex"])

    def test_portrait_discards_model_result_after_same_account_source_root_switch(self):
        self.source.rows = [
            {"id": f"m{index}", "side": "other", "kind": "text",
             "text": "合成历史消息" * 10, "senderId": "friend",
             "_sort": [index, "shard", index]}
            for index in range(1, 71)
        ]
        selected = self.activate(context_tokens=4096)
        current = self.pin_synthetic_source_root()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.analyzer.model_portrait

        def delayed(*args):
            entered.set()
            release.wait(timeout=3)
            return original(*args)

        self.analyzer.model_portrait = delayed
        self.backend.start_model_portrait("account-a", "friend")
        self.assertTrue(entered.wait(timeout=1))
        current["root"] = "source-root-b"
        release.set()
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertEqual(len(self.analyzer.portrait_calls), 1,
                         "a changed root must stop before a second paid portrait batch")
        saved = self.backend.store_factory("account-a", self.source.workdir).api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        self.assertEqual((saved["batchIndex"], saved["processed"], saved["complete"]),
                         (0, 0, False), "a changed root must not checkpoint the model result")

    def test_axes_refresh_discards_result_after_same_account_source_root_switch(self):
        selected = self.activate()
        current = self.pin_synthetic_source_root()
        self.backend.start_model_portrait("account-a", "friend")
        self.assertEqual(self.wait_portrait()["job"]["status"], "done")
        store = self.backend.store_factory("account-a", self.source.workdir)
        before = store.api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.analyzer.refresh_portrait_axes

        def delayed(*args):
            entered.set()
            release.wait(timeout=3)
            return original(*args)

        self.analyzer.refresh_portrait_axes = delayed
        self.backend.start_model_portrait("account-a", "friend", refresh_axes=True)
        self.assertTrue(entered.wait(timeout=1))
        current["root"] = "source-root-b"
        release.set()
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        after = store.api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        self.assertEqual(after["portrait"], before["portrait"],
                         "the axes result must not overwrite an unchanged account/workdir after root switch")
        self.assertEqual(after["batchIndex"], before["batchIndex"])

    def test_portrait_retries_one_invalid_output_without_resetting_progress(self):
        self.activate()
        self.analyzer.fail_portrait_invalid_once = True
        with patch("backend_service.API_MODEL_RETRY_SECONDS", .01):
            self.backend.start_model_portrait("account-a", "friend")
            done = self.wait_portrait()
        self.assertEqual(done["job"]["status"], "done")
        self.assertEqual(done["progress"]["processed"], 3,
                         "the retry must replay the same batch, not restart from zero")
        self.assertEqual(len(self.analyzer.portrait_calls), 2,
                         "one bounded retry for a format-invalid batch")

    def test_portrait_retry_checks_account_before_another_model_call(self):
        selected = self.activate()
        self.analyzer.fail_portrait_invalid_once = True
        with patch("backend_service.API_MODEL_RETRY_SECONDS", .5):
            self.backend.start_model_portrait("account-a", "friend")
            job_key = ("account-a", "friend", selected["sourceId"], "friend")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                job = self.backend.api_portrait_jobs.get(job_key)
                if job and job.get("retry"):
                    break
                time.sleep(.01)
            self.assertIsNotNone(job.get("retry"), "first model failure should enter backoff")
            self.source.account = "account-b"
            deadline = time.monotonic() + 3
            while self.backend.api_inflight and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        self.assertEqual(len(self.analyzer.portrait_calls), 1,
                         "a changed account must not pay for another model call")
        saved = self.backend.store_factory("account-a", self.source.workdir).api_portrait_get(
            "account-a", "friend", api_portrait_scope(selected["sourceId"]), "friend")
        self.assertEqual((saved["batchIndex"], saved["processed"], saved["complete"]),
                         (0, 0, False), "retry cancellation must leave the cursor unadvanced")

    def test_portrait_stops_after_ten_spaced_retries_without_advancing_checkpoint(self):
        self.activate()
        calls = []
        def invalid(*args):
            calls.append(1)
            raise RuntimeError("invalid-output")
        self.analyzer.model_portrait = invalid
        with patch("backend_service.API_MODEL_RETRY_SECONDS", .001):
            self.backend.start_model_portrait("account-a", "friend")
            failed = self.wait_portrait()
        self.assertEqual(failed["job"]["status"], "error")
        self.assertEqual(failed["job"]["error"], "invalid-output")
        self.assertEqual(len(calls), 11, "initial call plus ten retries")
        self.assertEqual(failed["progress"]["processed"], 0)

    def test_clear_one_api_source_deletes_only_it_and_leaves_it_usable(self):
        first = self.activate()
        self.backend.start_model_insights("account-a", "friend", 1)
        self.wait_done()
        self.backend.start_model_portrait("account-a", "friend")
        self.assertEqual(self.wait_portrait()["job"]["status"], "done")
        second = self.activate(model="other-model")
        self.backend.start_model_insights("account-a", "friend", 1)
        self.wait_done()
        before = {row["sourceId"]: row for row in self.backend.analysis_cache_status()["sources"]}
        self.assertGreater(before[first["sourceId"]]["messageCount"], 0)
        self.assertGreater(before[first["sourceId"]]["portraitCount"], 0)
        self.assertGreater(before[second["sourceId"]]["messageCount"], 0)
        cleared = self.backend.analysis_cache_clear("account-a", second["sourceId"])
        self.assertEqual(cleared["cleared"], True)
        self.assertNotIn("resumeRequired", cleared)
        after = {row["sourceId"]: row for row in self.backend.analysis_cache_status()["sources"]}
        self.assertEqual((after[second["sourceId"]]["messageCount"],
                          after[second["sourceId"]]["portraitCount"]), (0, 0))
        self.assertFalse(after[second["sourceId"]]["suspended"])
        self.assertGreater(after[first["sourceId"]]["messageCount"], 0)
        self.assertGreater(after[first["sourceId"]]["portraitCount"], 0)
        # The cleared source is immediately usable instead of hidden behind Restore.
        self.backend.start_model_insights("account-a", "friend", 1)
        self.assertEqual(self.wait_done()["job"]["status"], "done")

    def test_clear_is_bounded_and_discards_its_inflight_writer(self):
        self.activate()
        source_id = self.backend.active_model_source_id
        self.analyzer.entered = threading.Event()
        self.analyzer.release = threading.Event()
        self.addCleanup(self.analyzer.release.set)
        self.backend.start_model_insights("account-a", "friend", 2)
        self.assertTrue(self.analyzer.entered.wait(timeout=2))
        started = time.monotonic()
        cleared = self.backend.analysis_cache_clear("account-a", source_id)
        self.assertEqual(cleared["cleared"], True)
        self.assertLess(time.monotonic() - started, 5,
                        "a clear must be bounded, not block on the writer")
        # Releasing the writer must not write its now-stale result back.
        self.analyzer.release.set()
        deadline = time.monotonic() + 3
        while self.backend.api_inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.backend.api_inflight, 0)
        store = self.backend.store_factory("account-a", self.source.workdir)
        self.assertFalse(store.cache_suspended("account-a", source_id))
        result = self.backend.model_insights("friend")
        self.assertEqual(result["results"], {})
        self.assertFalse(result["suspended"])

    def test_clear_releases_guard_when_prior_suspension_read_fails(self):
        first = self.activate()
        source_id = first["sourceId"]
        _account, _workdir, store = self.backend._scoped_identity()
        store.register_api_source("account-a", source_id, "responses", "test-model")
        with patch.object(store, "cache_suspended", side_effect=RuntimeError("synthetic read failure")):
            with self.assertRaises(RuntimeError):
                self.backend.analysis_cache_clear("account-a", source_id)
        # A failed read must not leave the in-progress guard (or a suspension) stuck.
        self.assertNotIn(("account-a", source_id), self.backend.cache_clear_in_progress)
        self.assertFalse(store.cache_suspended("account-a", source_id))
        self.backend.analysis_cache_clear("account-a", source_id)

    def test_api_clear_is_source_scoped_and_does_not_wait_on_other_work(self):
        self.activate()
        self.analyzer.entered = threading.Event()
        self.analyzer.release = threading.Event()
        self.addCleanup(self.analyzer.release.set)
        self.backend.start_model_insights("account-a", "friend", 2)
        self.assertTrue(self.analyzer.entered.wait(timeout=2))
        store = self.backend.store_factory("account-a", self.source.workdir)
        other = "b" * 32
        store.register_api_source("account-a", other, "responses", "other-model")
        started = time.monotonic()
        cleared = self.backend.analysis_cache_clear("account-a", other)
        self.assertEqual(cleared["cleared"], True)
        self.assertEqual(self.backend.source.profile_invalidations, 0,
                         "an API source cache clear must not invalidate local profile metadata")
        self.assertLess(time.monotonic() - started, 5,
                        "clearing one source must not wait on another source's in-flight work")
        self.assertEqual(self.backend.api_inflight, 1,
                         "the unrelated writer is still running")
        self.assertNotIn(("account-a", other), self.backend.cache_clear_in_progress)
        self.analyzer.release.set()

    def test_clear_reclaims_rows_left_suspended_by_the_old_behavior(self):
        account = "account-a"
        legacy = ResultStore(Path(self.temp.name) / f"{account}.sqlite3")
        with legacy.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO analysis_cache_suspended_v1 VALUES (?,?)",
                         (account, LOCAL_SOURCE_ID))
        local = next(row for row in self.backend.analysis_cache_status()["sources"]
                     if row["sourceId"] == LOCAL_SOURCE_ID)
        self.assertFalse(local["suspended"])
        self.assertFalse(self.backend.store_factory(account, self.source.workdir)
                         .cache_suspended(account, LOCAL_SOURCE_ID))

    def test_local_clear_removes_only_current_account_rows_and_leaves_source_usable(self):
        api = self.activate()
        self.backend.start_model_insights("account-a", "friend", 1)
        self.wait_done()
        self.backend.model_source_activate({"mode": "local"})
        account, _workdir, store = self.backend._scoped_identity()
        with store.connect() as conn:
            conn.execute("INSERT INTO results_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (account, "friend", "local-id", 1, "shard", 1, "friend", "other",
                          "{}", 0.1, "synthetic"))
            conn.execute("INSERT INTO summary_v1 VALUES (?,?,?,?,?,?,?,?)",
                         (account, "friend", "synthetic", 1, 0.1, 0.0, 0, "{}"))
            conn.execute("INSERT INTO results_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         ("account-b", "friend", "other-account-id", 1, "shard", 1,
                          "friend", "other", "{}", 0.1, "synthetic"))
        self.assertEqual(self.backend.analysis_cache_clear(account, LOCAL_SOURCE_ID)["cleared"], True)
        # N2: clearing the local analysis must actually invalidate the source's profile
        # metadata through the public entry, not a private attribute it may not have.
        self.assertEqual(self.backend.source.profile_invalidations, 1)
        with store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM results_v2 WHERE account=?",
                                          (account,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM summary_v1 WHERE account=?",
                                          (account,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM results_v2 WHERE account='account-b'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM api_insights_v1 WHERE account=?",
                                          (account,)).fetchone()[0], 1)
        self.assertFalse(store.cache_suspended(account, LOCAL_SOURCE_ID))
        self.assertFalse(next(row for row in self.backend.analysis_cache_status()["sources"]
                              if row["sourceId"] == LOCAL_SOURCE_ID)["suspended"])

    def _activate_later(self, errors):
        try:
            self.activate()
        except Exception as exc:
            errors.append(exc)


class ProfileOverviewCacheTests(unittest.TestCase):
    def test_new_highwater_refreshes_members_and_counts(self):
        members = [(1, "old-member")]
        counts = [(1, 5)]
        class Conn:
            def execute(self, sql):
                return counts if "GROUP BY real_sender_id" in sql else members
            def close(self):
                pass
        class Reader:
            account = "synthetic-account"
            messages_ready = True
            def _msg_conns(self, _user):
                return [(Conn(), "Msg_" + "a" * 32)]
        source = WeChatSource(factory=object())
        source._contacts = lambda _db: {}
        source.db = Reader()
        first = source.profile_overview("room@chatroom", highwater=(10, "shard", 1))
        self.assertEqual(first["count"], 5)
        members.append((2, "new-member"))
        counts.append((2, 1))
        second = source.profile_overview("room@chatroom", "new-member", highwater=(11, "shard", 2))
        self.assertEqual(second["count"], 1)
        self.assertIn("new-member", [item["id"] for item in second["members"]])

    def test_counts_do_not_cross_account_roots_with_shared_workdir(self):
        scans = []
        class Conn:
            def execute(self, sql):
                if "GROUP BY real_sender_id" in sql:
                    scans.append(1)
                    return [(1, 5)]
                return [(1, "friend")]
            def close(self):
                pass
        class Reader:
            account = "synthetic-account"
            messages_ready = True
            workdir = "synthetic-shared-workdir"
            def __init__(self, account_dir):
                self.account_dir = str(account_dir)
            def _msg_conns(self, _user):
                return [(Conn(), "Msg_" + "a" * 32)]
        source = WeChatSource(factory=object())
        source._contacts = lambda _db: {}
        source.db = Reader("synthetic-root-one")
        with patch("wechat_source.contact_display", return_value={"name": "synthetic"}):
            source.profile_overview("room@chatroom")
            source._release_db()
            source.db = Reader("synthetic-root-two")
            source.profile_overview("room@chatroom")
        self.assertEqual(len(scans), 2)

    def test_group_counts_survive_same_account_reader_refresh_but_not_account_forget(self):
        scans = []
        class Conn:
            def execute(self, sql):
                if "GROUP BY real_sender_id" in sql:
                    scans.append(1)
                    return [(1, 5)]
                return [(1, "friend")]
            def close(self):
                pass
        class Reader:
            account = "synthetic-account"
            messages_ready = True
            def _msg_conns(self, _user):
                return [(Conn(), "Msg_" + "a" * 32)]
        source = WeChatSource(factory=object())
        source._contacts = lambda _db: {}
        source.db = Reader()
        with patch("wechat_source.contact_display", return_value={"name": "synthetic"}):
            first = source.profile_overview("room@chatroom", highwater=(10, "shard", 1))
            source._release_db()
            source.db = Reader()
            second = source.profile_overview("room@chatroom", highwater=(10, "shard", 1))
            self.assertEqual((first["count"], second["count"], len(scans)), (5, 5, 1))
            source.forget_account("synthetic-account")
            source.db = Reader()
            source.profile_overview("room@chatroom", highwater=(10, "shard", 1))
            self.assertEqual(len(scans), 2)


if __name__ == "__main__":
    unittest.main()

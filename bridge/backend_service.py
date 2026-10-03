"""Application service coordinating account-scoped analysis and background jobs.

WeChat IO, Node process IO, and SQLite persistence live behind adapters/repositories.
Constructor injection (source, analyzer, store_factory) remains supported.
"""
from __future__ import annotations

import hashlib
import heapq
import inspect
import itertools
import json
import math
import queue
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path

import message_input
from api_tasks import ApiTaskCoordinator
from analysis_scheduler import FairAnalysisQueue
from analysis_control import AnalysisInterrupted, AnalysisBudgetExhausted, require_running
from backend_contracts import (
    API_INSIGHT_RETRYABLE, API_INSIGHT_RETRY_MAX, API_INSIGHT_RETRY_SECONDS,
    API_JOB_CACHE_LIMIT, API_MODEL_RETRYABLE, API_MODEL_RETRY_MAX,
    API_MODEL_RETRY_SECONDS, API_PORTRAIT_COUNT_KEYS, API_PORTRAIT_INVENTORY_CACHE_BYTES,
    API_PORTRAIT_PIECE_CHARS, AccountChangedError, AccountUnavailableError,
    FORECAST_CACHE_LIMIT, FORECAST_SOURCE_WINDOW,
    ForecastRequestError, MAX_ISSUED_IMAGES,
    MODEL_CONNECTOR_ERRORS, ROOT, affinity_from_progress,
    api_insight_scope, api_portrait_add_counts, api_portrait_plan,
    api_portrait_resume_anchor, api_portrait_scope, api_portrait_tail_hashes,
    api_portrait_wire_chars, mbti_from_totals, model_source_failure,
    mood_from_progress, normalize_api_insight, scope_rank, valid_api_portrait,
)
from conversation_selection import ConversationSelectionStore, _session_id
from history_browser import browse as browse_history, saved_results as saved_history_results, search as search_history
from message_results import validate_fine_result, validate_portrait_result
from model_source import LOCAL_SOURCE_ID, ModelSourceStore, ModelSourceUnavailable, connection_values
from node_analysis import NodeAnalysis
from product_profile import current_product
from profile_signals import keywords_from_counts, summary_from_aggregate
from profile_state import empty_state as empty_profile_state, traits_from_state
from result_store import project_result_store
from wechat_source import WeChatSource
from qq_history_browser import browse as browse_qq_history, search as search_qq_history
from analysis_targets import (SELF_SUBJECT, is_group as source_is_group, subject as target_subject,
                              message_subject, labels_eligible, target as matches_target, portrait_version)

PRODUCT = current_product()


def trusted_source_kind(source):
    """A source declares its own kind; anything undeclared is unknown, never wechat."""
    kind = getattr(source, "kind", None)
    return kind if kind in message_input.SOURCE_KINDS else "unknown"


def published_analysis_read(method):
    """Read one QQ result generation across the several repository calls in a DTO."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if self.source_kind != "qq":
            return method(self, *args, **kwargs)
        account, _workdir, base = self._scoped_identity()
        with self.source.read_library(account), base.profile_lock:
            return method(self, *args, **kwargs)
    return guarded


def api_published_read(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if self.source_kind != "qq":
            return method(self, *args, **kwargs)
        # API workers use API lock -> source -> result store; preserve that order.
        with self.api_lock:
            account, _workdir, base = self._scoped_identity()
            with self.source.read_library(account), base.profile_lock:
                return method(self, *args, **kwargs)
    return guarded


class Backend:
    def __init__(self, source, analyzer=None, store_factory=None, model_source_store=None,
                 selection_store=None):
        self.source = source
        self.source_kind = trusted_source_kind(source)
        self.analyzer = analyzer or NodeAnalysis()
        self.api_analyzer = NodeAnalysis(api_only=True) if analyzer is None else analyzer
        # Long portrait generations must not hold up interactive message labels.
        self.api_portrait_analyzer = NodeAnalysis(api_only=True) if analyzer is None else analyzer
        # Discovery and connection probes must not wait behind a long portrait job.
        self.api_probe_analyzer = NodeAnalysis(api_only=True) if analyzer is None else analyzer
        self.model_source_store = model_source_store or ModelSourceStore(
            PRODUCT.state_dir("real-client-runtime", ROOT) / "api-model-source.json", root=ROOT,
            legacy_path=PRODUCT.state_dir("real-client-runtime", ROOT) / "model-source.json")
        # API chat insights have their own source-scoped cache. The local Laya
        # portrait/affinity worker keeps its existing analysis version.
        # One ApiTaskCoordinator owns the lock/condition, the three registries and
        # the inflight count. The legacy attributes below proxy the same objects
        # (never copies); model_source_revision stays a model-selection version and
        # is never used as an API cancellation epoch.
        self.api_tasks = ApiTaskCoordinator()
        self.api_lock = self.api_tasks.lock
        self.api_condition = self.api_tasks.condition
        self.api_jobs = self.api_tasks.insight_jobs
        self.api_portrait_jobs = self.api_tasks.portrait_jobs
        self.api_portrait_inventory_jobs = self.api_tasks.inventory_jobs
        self.model_source_revision = 0
        self.active_model_source_mode = "local"
        self.active_model_source_id = LOCAL_SOURCE_ID
        self.active_api_config = None
        if analyzer is None or model_source_store is not None:
            try:
                selected = self.model_source_store.saved_selection()
                if selected["selectedMode"] == "api" and selected["api"]:
                    self.active_model_source_mode = "api"
                    self.active_model_source_id = selected["sourceId"]
                    self.active_api_config = {key: selected["api"][key]
                                              for key in ("protocol", "baseUrl", "model", "contextTokens")}
            except ModelSourceUnavailable:
                # A damaged encrypted profile must not prevent local WeChat access.
                pass
        self.store_factory = store_factory or self._project_store
        self.selection_store = selection_store or ConversationSelectionStore(
            PRODUCT.state_dir("real-client-data", ROOT))
        self.stores = {}
        self.qq_analysis_stores = {}
        self.qq_api_stores = {}
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.request_condition = threading.Condition()
        self.active_requests = 0
        self.closing = False
        self.cache_clear_in_progress = set()
        self.account_clear_paused = False
        self.performance = {}
        self.priority_recent = {}
        self.recent_windows = {}
        self.incremental_recheck = set()
        self.history_iterators = {}
        self.history_iterator_modes = {}
        self.quoted_backfill_iterators = {}
        self.forecast_cache = OrderedDict()
        self.forecast_lock = threading.Lock()
        self.forecast_flights = {}
        self.focused_key = None
        self.tasks = FairAnalysisQueue(lambda:self.focused_key)
        self.task_serial = itertools.count()
        self.batch_engine = None
        if callable(getattr(self.analyzer, "analyze_batch", None)):
            from batch_engine import BatchEngine
            self.batch_engine = BatchEngine(self)
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    @contextmanager
    def request_lease(self):
        """Track HTTP requests. A source-scoped cache clear must not block unrelated reads."""
        with self.request_condition:
            if self.closing:
                raise RuntimeError("bridge is closing")
            self.active_requests += 1
        try:
            yield
        finally:
            with self.request_condition:
                self.active_requests -= 1
                self.request_condition.notify_all()

    def pause_for_account_clear(self, account):
        """Drain this bridge's work while retaining a way to resume after a failed clear."""
        with self.request_condition:
            if self.closing:
                raise RuntimeError("bridge is closing")
            self.closing = True
        # API insight jobs outlive their HTTP request. Drain them before
        # touching this account's SQLite file.
        with self.api_condition:
            self._cancel_api_source_work_locked()
            if not self.api_tasks.wait_for_idle(200):
                raise RuntimeError("API insight requests did not finish")
        self.tasks.put((99, next(self.task_serial), None))
        with self.request_condition:
            if not self.request_condition.wait_for(lambda: self.active_requests == 0, timeout=200):
                raise RuntimeError("bridge requests did not finish")
        self.worker_thread.join(timeout=200)
        if self.worker_thread.is_alive():
            raise RuntimeError("bridge worker did not stop")
        self.account_clear_paused = True
        try:
            forget = getattr(self.source, "forget_account", None)
            if callable(forget):
                forget(account)
        except Exception:
            self.resume_after_failed_account_clear()
            raise

    def resume_after_failed_account_clear(self):
        """Restore this bridge if scoped file removal failed after it was drained."""
        with self.request_condition:
            if not self.account_clear_paused or self.active_requests or self.worker_thread.is_alive():
                raise RuntimeError("bridge cannot safely resume")
        for iterator in self.history_iterators.values():
            try:
                iterator.close()
            except Exception:
                pass
        self.stores.clear()
        with self.jobs_lock:
            self.jobs.clear()
            self.priority_recent.clear()
            self.recent_windows.clear()
            self.incremental_recheck.clear()
        self.history_iterators.clear()
        self.history_iterator_modes.clear()
        self.quoted_backfill_iterators.clear()
        self.performance.clear()
        self.forecast_cache.clear()
        self.forecast_flights.clear()
        with self.api_lock:
            self.api_tasks.clear_insight_jobs()
        self.focused_key = None
        self.tasks = FairAnalysisQueue(lambda:self.focused_key)
        self.task_serial = itertools.count()
        if self.batch_engine:
            from batch_engine import BatchEngine
            self.batch_engine = BatchEngine(self)
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()
        with self.request_condition:
            self.account_clear_paused = False
            self.closing = False

    def shutdown(self, account=None):
        """Stop this bridge and model on normal exit or after an active-account clear."""
        with self.request_condition:
            first = not self.closing
            paused = self.account_clear_paused
            self.closing = True
            self.account_clear_paused = False
        coordinator = getattr(self, "qq_sync", None)
        if coordinator is not None:
            coordinator.close()
        with self.api_condition:
            self._cancel_api_source_work_locked()
            if not self.api_tasks.wait_for_idle(200):
                raise RuntimeError("API insight requests did not finish")
        if first or paused:
            if account is not None:
                forget = getattr(self.source, "forget_account", None)
                if callable(forget):
                    forget(account)
            close_source = getattr(self.source, "close", None)
            if callable(close_source):
                close_source()
            if first:
                self.tasks.put((99, next(self.task_serial), None))
        with self.request_condition:
            if not self.request_condition.wait_for(lambda: self.active_requests == 0, timeout=200):
                raise RuntimeError("bridge requests did not finish")
        self.worker_thread.join(timeout=200)
        if self.worker_thread.is_alive():
            raise RuntimeError("bridge worker did not stop")
        close_model = getattr(self.analyzer, "close", None)
        if callable(close_model):
            close_model()
        if self.api_analyzer is not self.analyzer:
            close_api = getattr(self.api_analyzer, "close", None)
            if callable(close_api):
                close_api()
        if self.api_probe_analyzer not in (self.analyzer, self.api_analyzer):
            close_probe = getattr(self.api_probe_analyzer, "close", None)
            if callable(close_probe):
                close_probe()
        if self.api_portrait_analyzer not in (self.analyzer, self.api_analyzer,
                                              self.api_probe_analyzer):
            close_portrait = getattr(self.api_portrait_analyzer, "close", None)
            if callable(close_portrait):
                close_portrait()
        self.stores.clear()
        self.forecast_cache.clear()

    def _task_priority(self, task, interactive=False):
        focused = task[0] == self.focused_key
        recent = interactive or task[1] == "recent-window"
        if task[1] == "batch-subject":
            active = self.batch_engine and self.batch_engine.focused_member == (task[0], task[2])
            return 2 if focused and active else 3
        # A newly requested visible window should run after the current model
        # call, even when a different conversation's baseline is focused.
        return (0 if focused else 1) if recent else (2 if focused else 3)

    def _enqueue(self, task, interactive=False):
        if self.closing:
            return
        self.tasks.put((self._task_priority(task, interactive), next(self.task_serial), task))

    def _focus(self, key):
        """Prioritize the visible conversation's baseline without restarting any iterator."""
        with self.jobs_lock:
            self.focused_key = key
            with self.tasks.mutex:
                for index, (priority, serial, task) in enumerate(self.tasks.queue):
                    priority = self._task_priority(task)
                    self.tasks.queue[index] = (priority, serial, task)
                heapq.heapify(self.tasks.queue)

    def _interactive_waiting(self):
        with self.tasks.mutex:
            return bool(self.tasks.queue and self.tasks.queue[0][0] <= 1)

    @staticmethod
    def _project_store(account, _workdir):
        return project_result_store(account, _workdir)

    def _selection_account(self):
        """Resolve the real login without requiring message shards or analysis state."""
        if self.closing:
            raise AccountUnavailableError()
        verified = getattr(self.source, "verified_identity", None)
        if callable(verified):
            account, _workdir = verified(messages=False)
        else:
            account, _workdir = self.source.identity()
        if not isinstance(account, str) or not account:
            raise AccountUnavailableError()
        return account

    def conversation_selection(self):
        account = self._selection_account()
        state = self.selection_store.get(account)
        if self._selection_account() != account:
            raise AccountChangedError()
        return state

    def set_conversation_selected(self, expected_account, session, selected):
        if not isinstance(expected_account, str) or not expected_account:
            raise ValueError("invalid expected account")
        _session_id(session)
        if type(selected) is not bool:
            raise ValueError("invalid selected")
        account = self._selection_account()
        if account != expected_account:
            raise AccountChangedError()
        if selected:
            metadata = self.source.sessions()
            if metadata.get("account") != account:
                raise AccountChangedError()
            if session not in {item.get("username") for item in metadata.get("sessions", [])}:
                raise ValueError("unknown session")
        elif session not in self.selection_store.get(account)["selectedSessions"]:
            raise ValueError("session not selected")
        if self._selection_account() != account:
            raise AccountChangedError()
        state = self.selection_store.set_selected(account, session, selected)
        coordinator = getattr(self, "qq_sync", None)
        if coordinator is not None:
            coordinator.selection_changed(account, session, selected)
        if self._selection_account() != account:
            raise AccountChangedError()
        return state

    def _scoped_identity(self):
        if self.closing:
            raise AccountUnavailableError()
        verified = getattr(self.source, "verified_identity", None)
        if callable(verified):
            account, workdir = verified(messages=True)
        else:
            ready = getattr(self.source, "require_messages_ready", None)
            if callable(ready):
                ready()
            account, workdir = self.source.identity()
        scope = (str(account), str(Path(workdir).resolve()))
        if scope not in self.stores:
            store = self.store_factory(account, workdir)
            normalize = getattr(store, "clear_legacy_suspensions", None)
            if callable(normalize):
                normalize(str(account))
            self.stores[scope] = store
        return scope[0], scope[1], self.stores[scope]

    def _identity(self):
        account, _, store = self._scoped_identity()
        return account, store

    def _assert_scope(self, scope):
        if self.closing:
            raise AccountChangedError()
        verified = getattr(self.source, "verified_identity", None)
        if callable(verified):
            account, workdir = verified(messages=True)
        else:
            ready = getattr(self.source, "require_messages_ready", None)
            if callable(ready):
                ready()
            account, workdir = self.source.identity()
        if (str(account), str(Path(workdir).resolve())) != scope:
            raise AccountChangedError()
        check = getattr(scope, "assert_current", None)
        if callable(check):
            check()

    def _qq_analysis_store(self, account, workdir, store):
        if self.source_kind != "qq":
            return None
        from qq_analysis_store import QQAnalysisStore
        key = (account, str(store.path))
        if key not in self.qq_analysis_stores:
            self.qq_analysis_stores[key] = QQAnalysisStore(self.source, store, account, workdir)
        return self.qq_analysis_stores[key]

    @staticmethod
    def _publish_local_analysis(store, *, complete=False):
        publish = getattr(store, "publish", None)
        if callable(publish):
            publish(complete=complete)

    def _qq_api_store(self, account, workdir, store):
        if self.source_kind != "qq":
            return None
        from qq_api_analysis_store import QQApiAnalysisStore
        key = (account, str(store.path))
        if key not in self.qq_api_stores:
            self.qq_api_stores[key] = QQApiAnalysisStore(self.source, store, account, workdir)
        return self.qq_api_stores[key]

    @staticmethod
    def _publish_api_analysis(store):
        if getattr(store, "component", None) in ("insights", "portrait", "inventory"):
            store.publish()

    @staticmethod
    def _inventory_key(account, workdir, user, subject, highwater, scope):
        key = (account, workdir, user, subject, highwater)
        stage = getattr(scope, "stage", None)
        return (*key, stage.revision) if stage is not None else key

    @staticmethod
    def _qq_insight_context(repository, base, user, source_id, window):
        from qq_api_analysis_store import insight_context_hash
        portrait_context = {}
        for subject in sorted({message_subject(repository.source,user,item) for item in window if labels_eligible(repository.source,item)}):
            if subject and not repository.status(user, source_id, 'portrait', subject)['stale']:
                saved=base.api_portrait_get(repository.account,user,api_portrait_scope(source_id),subject)
                if saved:
                    portrait_context[subject]=saved['portrait']['summary'][:80]
        return portrait_context, insight_context_hash(window, portrait_context)

    def _stored_progress(self, account, user, version, store):
        progress = store.progress(account, user, version)
        if self.batch_engine:
            saved = self.batch_engine.snapshot(account, user, version, store)
            if saved:
                progress = {**progress, "cursor": saved["cursor"], "context": saved["context"],
                            "complete": saved["complete"], "eligible": saved["state"]["count"]}
        return progress

    def health(self):
        state = "idle"
        error = None
        account = None
        # A capability query, not a WeChat type test or a `.db` peek: a connected QQ
        # source with no messages yet is a legal empty state, not a 503 (contract 1.1).
        if callable(getattr(self.source, "require_messages_ready", None)):
            try:
                account, _ = self.source.identity()
                state = "ready"
            except AccountUnavailableError as exc:
                state, error = "account-unavailable", str(exc)
            except Exception as exc:
                state, error = "error", str(exc)
        model = self.analyzer.model
        data = {"state": state}
        if account:
            data["account"] = account
        if error:
            data["error"] = error
        return {"ok": state not in ("error", "account-unavailable") and model["state"] not in ("error", "missing"), "data": data,
                "model": {"state": model["state"],
                          **({"provider": model["provider"]} if model.get("provider") in ("cpu", "webgpu") else {}),
                          **({"error": model["message"]} if model.get("message") and model["state"] != "ready" else {})},
                "version": "real-ui-1"}

    def runtime(self):
        return self.analyzer.runtime_status()

    def configure_runtime(self, provider):
        return self.analyzer.configure_runtime(provider)

    def local_model_status(self):
        return self.analyzer.local_model_status()

    def configure_local_model(self, value):
        return self.analyzer.configure_local_model(value)

    def model_source(self):
        with self.api_lock:
            return self.model_source_store.public(self.active_model_source_mode,
                                                  self.active_model_source_id, "active")

    def model_source_list(self, request):
        values = connection_values(request)
        key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                  values["apiKey"])
        try:
            reply = self.api_probe_analyzer.model_list(values["protocol"], values["baseUrl"], key)
        except Exception as exc:
            raise model_source_failure(exc, "model list unavailable") from exc
        if not isinstance(reply, dict) or type(reply.get("supported")) is not bool or not isinstance(
                reply.get("models"), list):
            raise ModelSourceUnavailable("model list unavailable")
        models = []
        seen = set()
        for item in reply["models"][:500]:
            if not isinstance(item, dict):
                raise ModelSourceUnavailable("model list unavailable")
            model_id = item.get("id")
            if not isinstance(model_id, str) or not 1 <= len(model_id) <= 256 or any(
                    ord(char) < 32 or ord(char) == 127 for char in model_id):
                raise ModelSourceUnavailable("model list unavailable")
            if model_id in seen:
                continue
            seen.add(model_id)
            name = item.get("name")
            if name is not None and (not isinstance(name, str) or len(name) > 256 or any(
                    ord(char) < 32 or ord(char) == 127 for char in name)):
                raise ModelSourceUnavailable("model list unavailable")
            context_tokens = item.get("contextTokens")
            if context_tokens is not None and (type(context_tokens) is not int or
                    not 4096 <= context_tokens <= 1000000):
                raise ModelSourceUnavailable("model list unavailable")
            models.append({"id": model_id, **({"name": name} if name else {}),
                           **({"contextTokens": context_tokens} if context_tokens else {})})
        return {"models": models if reply["supported"] else [], "supported": reply["supported"]}

    def model_source_test(self, request):
        values = connection_values(request, require_model=True)
        key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                  values["apiKey"])
        try:
            reply = self.api_probe_analyzer.model_test(values["protocol"], values["baseUrl"], key,
                                             values["model"])
        except Exception as exc:
            raise model_source_failure(exc, "model connection failed") from exc
        latency = reply.get("latencyMs") if isinstance(reply, dict) else None
        if (not isinstance(reply, dict) or reply.get("ok") is not True or
                type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0):
            raise ModelSourceUnavailable("model connection failed")
        return {"ok": True, "latencyMs": latency}

    @property
    def api_inflight(self):
        return self.api_tasks.inflight

    @api_inflight.setter
    def api_inflight(self, value):
        self.api_tasks.inflight = value

    @property
    def api_portrait_inventory_pieces(self):
        return self.api_tasks.inventory_pieces

    @api_portrait_inventory_pieces.setter
    def api_portrait_inventory_pieces(self, value):
        self.api_tasks.inventory_pieces = value

    def _cancel_api_source_work_locked(self):
        """Stop old-source model calls; saved portraits, cursors and the source-independent
        inventory stay intact. Only insight/portrait jobs are cancelled."""
        self.api_tasks.invalidate_models()
        seen = set()
        for analyzer in (self.api_analyzer, self.api_portrait_analyzer,
                         self.api_probe_analyzer):
            if analyzer is self.analyzer or id(analyzer) in seen:
                continue
            seen.add(id(analyzer))
            cancel = getattr(analyzer, "cancel", None)
            if callable(cancel):
                cancel()

    def model_source_activate(self, request):
        if request == {"mode": "local"}:
            with self.api_lock:
                was_api = self.active_model_source_mode == "api"
                self.model_source_store.save_local()
                self.active_model_source_mode = "local"
                self.active_model_source_id = LOCAL_SOURCE_ID
                self.active_api_config = None
                self.model_source_revision += 1
                if was_api:
                    self._cancel_api_source_work_locked()
                return self.model_source()
        if not isinstance(request, dict) or request.get("mode") != "api":
            raise ValueError("invalid model source request")
        values = connection_values({key: value for key, value in request.items() if key != "mode"},
                                   require_model=True)
        if values["contextTokens"] is None:
            raise ValueError("contextTokens required")
        with self.api_lock:
            key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                      values["apiKey"])
            revision = self.model_source_revision
            saved_profile = self.model_source_store.saved_selection()["api"]
            reuse_validated = (values["apiKey"] is None and key is not None and
                               saved_profile is not None and
                               (saved_profile["protocol"], saved_profile["baseUrl"],
                                saved_profile["model"]) ==
                               (values["protocol"], values["baseUrl"], values["model"]))
        if not reuse_validated:
            try:
                tested = self.api_probe_analyzer.model_test(values["protocol"], values["baseUrl"], key,
                                                  values["model"])
            except Exception as exc:
                raise model_source_failure(exc, "model connection failed") from exc
            if not isinstance(tested, dict) or tested.get("ok") is not True:
                raise ModelSourceUnavailable("model connection failed")
        with self.api_lock:
            if self.closing or revision != self.model_source_revision:
                raise ModelSourceUnavailable("model source changed during connection test")
            saved = self.model_source_store.saved_selection()
            previous_context = saved["api"].get("contextTokens") if saved["api"] else None
            source_id = self.model_source_store.save_api(
                values["protocol"], values["baseUrl"], values["model"], key,
                context_tokens=values["contextTokens"])
            changed_source = (self.active_model_source_mode != "api" or
                              self.active_model_source_id != source_id or
                              previous_context != values["contextTokens"])
            self.active_model_source_mode = "api"
            self.active_model_source_id = source_id
            self.active_api_config = {field: values[field] for field in
                                      ("protocol", "baseUrl", "model", "contextTokens")}
            if changed_source:
                self._cancel_api_source_work_locked()
            if previous_context != values["contextTokens"]:
                self.api_tasks.drop_portrait_error(source_id)
            self.model_source_revision += 1
            return self.model_source()

    def model_source_clear_key(self, request):
        if request != {}:
            raise ValueError("invalid model source request")
        with self.api_lock:
            was_api = self.active_model_source_mode == "api"
            self.model_source_store.clear_key()
            self.active_model_source_mode = "local"
            self.active_model_source_id = LOCAL_SOURCE_ID
            self.active_api_config = None
            self.model_source_revision += 1
            if was_api:
                self._cancel_api_source_work_locked()
            return self.model_source()

    @api_published_read
    def model_insights(self, user, ids=None, around=None):
        if ids is not None and (not isinstance(ids, list) or len(ids) > 500 or
                any(not isinstance(item, str) or not 1 <= len(item) <= 200 or
                    any(ord(char) < 32 or ord(char) == 127 for char in item) for item in ids) or
                len(set(ids)) != len(ids)):
            raise ValueError("invalid API insight ids")
        account, workdir, store = self._scoped_identity()
        if around is not None and (not isinstance(around, str) or not 1 <= len(around) <= 1024):
            raise ValueError("invalid API history anchor")
        with self.api_lock:
            mode, source_id = self.active_model_source_mode, self.active_model_source_id
            job = dict(self.api_jobs.get((account, user, source_id)) or
                       {"id": None, "status": "idle", "total": 0, "processed": 0})
        results = {}
        suspended = store.cache_suspended(account, source_id) if mode == "api" else False
        if mode == "api":
            results = store.api_insight_view(account, user, api_insight_scope(source_id),
                                             ids=ids, limit=500)
            self._assert_scope((account, workdir))
            repository = self._qq_api_store(account, workdir, store)
            if repository:
                revision = repository.status(user, source_id, "insights", user)
                try:
                    window = (self._browse_history(account, user, around=around, limit=500,
                              max_issued_images=MAX_ISSUED_IMAGES)["messages"] if around else
                              self.source.messages(user, 500))
                except ValueError as exc:
                    if str(exc) != "stale-history-cursor":
                        raise
                    return {"account": account, "sourceId": source_id, "results": {}, "previousResults": results,
                            "job": {"id": None, "status": "idle"}, "suspended": suspended,
                            **revision, "hasPublishedAnalysis": bool(results), "refreshRequired": True}
                _portrait, context_hash = self._qq_insight_context(repository, store, user, source_id, window)
                known = store.api_insight_basis_ids(account, user, api_insight_scope(source_id),
                    list(results), revision["dataRevision"], context_hash)
                previous = {key: value for key, value in results.items() if key not in known}
                results = {key: value for key, value in results.items() if key in known}
                if (job.get("dataRevision") != revision["dataRevision"] or
                        job.get("status") not in ("queued", "running") and job.get("contextHash") != context_hash):
                    job = {"id": None, "status": "idle", "total": 0, "processed": 0}
                elif job.get("contextHash") != context_hash:
                    job = {**job, "partialText": "", "targetIds": []}
                return {"account": account, "sourceId": source_id, "results": results, "previousResults": previous,
                        "job": job, "suspended": suspended, **revision,
                        "stale": revision["stale"] or bool(previous), "hasPublishedAnalysis": bool(results or previous),
                        "contextHash": context_hash}
        return {"account": account, "sourceId": source_id, "results": results, "job": job,
                "suspended": suspended}

    @api_published_read
    def start_model_insights(self, requested_account, user, limit, target_ids=None, around=None):
        started = time.perf_counter()
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid API insight limit")
        if target_ids is not None and (not isinstance(target_ids, list) or
                not 1 <= len(target_ids) <= limit or
                any(not isinstance(item, str) or not 1 <= len(item) <= 200 or
                    any(ord(char) < 32 or ord(char) == 127 for char in item) for item in target_ids) or
                len(set(target_ids)) != len(target_ids)):
            raise ValueError("invalid API insight targets")
        if around is not None and (not isinstance(around, str) or not around or
                len(around) > 1024 or target_ids is None):
            raise ValueError("invalid API history anchor")
        account, workdir, store = self._scoped_identity()
        if account != requested_account:
            raise AccountChangedError()
        if self.source_kind=="qq":require_running(store,account,user)
        with self.api_lock:
            if self.active_model_source_mode != "api" or not self.active_api_config:
                raise ModelSourceUnavailable("API model is not active")
            source_id = self.active_model_source_id
            config = dict(self.active_api_config)
            api_key = self.model_source_store.resolve_key(config["protocol"], config["baseUrl"], None)
        window = (self._browse_history(account, user, around=around, limit=500,
                                 max_issued_images=MAX_ISSUED_IMAGES)["messages"]
                  if around is not None else self.source.messages(user, 500))
        self._assert_scope((account, workdir))
        source_scope = (account, workdir)
        repository = self._qq_api_store(account, workdir, store)
        if repository:
            base = store
            if base.cache_suspended(account, source_id):
                return {"account": account, "sourceId": source_id,
                        "job": {"id": None, "status": "suspended", "total": 0, "processed": 0}}
            store = repository.bind(user, source_id, "insights", user)
            source_scope = store.scope
            store.portrait_context, store.context_hash = self._qq_insight_context(repository, base, user, source_id, window)
        with self.api_lock:
            if (self.closing or self.active_model_source_mode != "api" or
                    self.active_model_source_id != source_id):
                raise ModelSourceUnavailable("model source changed")
            if store.cache_suspended(account, source_id):
                return {"account": account, "sourceId": source_id,
                        "job": {"id": None, "status": "suspended", "total": 0, "processed": 0}}
            store.register_api_source(account, source_id, config["protocol"], config["model"])
            text_window = [item for item in window if item["side"] in ("self", "other") and
                           item["kind"] == "text" and isinstance(item["text"], str)]
            eligible = [item for item in text_window if labels_eligible(self.source,item) and item["text"].strip()]
            if target_ids is None:
                selected = eligible[-limit:]
            else:
                by_id = {item["id"]: item for item in eligible}
                if any(item not in by_id for item in target_ids):
                    raise ValueError("API insight target is no longer recent")
                selected = [by_id[item] for item in target_ids]
            # Keep the local result store as the session history.  A new batch
            # carries the current message window, but only ids without a saved
            # result are sent to the provider.  This is the same separation as
            # OpenCode's message history: the client owns history, while the
            # provider decides how much of it fits its context window.
            known = store.api_insight_known(account, user, api_insight_scope(source_id),
                                            [item["id"] for item in selected])
            job_key = (account, user, source_id)
            current = self.api_jobs.get(job_key)
            if (current and current["status"] in ("queued", "running") and
                    (not repository or current.get("dataRevision") == store.revision)):
                return {"account": account, "sourceId": source_id, "job": dict(current)}
            pending = [item for item in selected if item["id"] not in known]
            wire = []
            if pending:
                pending_ids = {item["id"] for item in pending}
                # Every submitted batch includes the current local message history;
                # only targetIds are incremental. There is no previous_response_id
                # or application-side context slicing.
                batch_window = text_window
                prepared = {item["id"]: message_input.prepare_item(
                    item, account_id=account, conversation_id=user, source_kind=self.source_kind)
                    for item in pending}
                wire = []
                for item in batch_window:
                    entry = {"id": item["id"],
                             "sender": "SELF" if item["side"] == "self" else "OTHER",
                             "text": item["text"]}
                    if self.source_kind=='qq':
                        entry['conversationKind']='group' if source_is_group(self.source,user) else 'friend'
                        if entry['conversationKind']=='group':
                            from qq_roles import group_context
                            entry['groupContext']=group_context(item)
                    if item["id"] in prepared:
                        entry["inputMeta"] = prepared[item["id"]]["inputMeta"]
                    wire.append(entry)
            job = {"id": uuid.uuid4().hex, "status": "queued",
                   "total": len(selected), "processed": len(selected) - len(pending),
                   "startedAtMs": int(time.time() * 1000),
                   # The partial response exists only in this in-memory job. It is
                   # never written to SQLite or included in a persisted result.
                   "targetIds": [item["id"] for item in pending],
                   "partialText": "",
                   "timings": {"firstBodyMs": None},
                   **({"dataRevision": store.revision, "contextHash": store.context_hash} if repository else {})}
            self.api_jobs[job_key] = job
            if len(self.api_jobs) > API_JOB_CACHE_LIMIT:
                for old_key, old_job in list(self.api_jobs.items()):
                    if len(self.api_jobs) <= API_JOB_CACHE_LIMIT:
                        break
                    if old_key != job_key and old_job["status"] not in ("queued", "running"):
                        del self.api_jobs[old_key]
            job["timings"]["prepareMs"] = round((time.perf_counter() - started) * 1000, 3)
            if not pending:
                self._publish_api_analysis(store)
                job["status"] = "done"
                job["timings"]["totalMs"] = round((time.perf_counter() - started) * 1000, 3)
                return {"account": account, "sourceId": source_id, "job": dict(job)}
            self.api_tasks.begin()
            thread = threading.Thread(target=self._run_model_insights,
                                      args=(job_key, job, source_scope, store, config,
                                            api_key, wire, pending), daemon=True)
            try:
                thread.start()
            except Exception:
                self.api_tasks.finish()
                job["status"] = "error"
                job["error"] = "model-analysis-failed"
                raise
            return {"account": account, "sourceId": source_id, "job": dict(job)}

    def _run_model_insights(self, job_key, job, scope, store, config, api_key, wire, pending):
        try:
            with self.api_lock:
                if (self.closing or self.active_model_source_mode != "api" or
                        self.active_model_source_id != job_key[2]):
                    job["status"] = "error"
                    job["error"] = "model-source-changed"
                    return
                job["status"] = "running"
                queued_at_ms = job.get("startedAtMs")
                if isinstance(queued_at_ms, (int, float)):
                    job.setdefault("timings", {})["queueMs"] = max(
                        0.0, round(time.time() * 1000 - queued_at_ms, 3))
            contexts = {}
            for message in pending:
                subject = message_subject(self.source,job_key[1],message)
                if not subject:
                    continue
                if subject not in contexts:
                    if getattr(store, "component", None) == "insights":
                        contexts[subject] = store.portrait_context.get(subject,'')
                    else:
                        saved = store.api_portrait_get(job_key[0], job_key[1],
                                                       api_portrait_scope(job_key[2]), subject)
                        contexts[subject] = saved["portrait"]["summary"][:80] if saved else ""
                if contexts[subject]:
                    next(item for item in wire if item["id"] == message["id"])[
                        "portraitContext"] = contexts[subject]
            with self.api_lock:
                if (self.closing or self.active_model_source_mode != "api" or
                        self.active_model_source_id != job_key[2]):
                    raise RuntimeError("model-source-changed")
            self._assert_scope(scope)
            target_ids = [item["id"] for item in pending]
            for attempt in range(API_INSIGHT_RETRY_MAX + 1):
                try:
                    self._assert_scope(scope)
                    with self.api_lock:
                        if self.api_jobs.get(job_key) is job:
                            # A retry is a fresh provider turn. Do not let a
                            # failed turn's labels remain visible beside the
                            # newest streamed text.
                            job["partialText"] = ""
                            job.setdefault("timings", {})["firstBodyMs"] = None
                    provider_started = time.perf_counter()
                    stream_parts = []

                    def on_delta(delta):
                        if not isinstance(delta, str) or not delta:
                            return
                        with self.api_lock:
                            if (self.api_jobs.get(job_key) is not job or
                                    job.get("status") not in ("queued", "running")):
                                return
                            stream_parts.append(delta)
                            job["partialText"] = "".join(stream_parts)
                            timings = job.setdefault("timings", {})
                            if timings.get("firstBodyMs") is None:
                                timings["firstBodyMs"] = round(
                                    (time.perf_counter() - provider_started) * 1000, 3)

                    # Test doubles and legacy analyzers may keep their six-argument
                    # contract. Pass the optional callback whenever the adapter
                    # explicitly exposes it, including streaming test doubles.
                    model_insights = self.api_analyzer.model_insights
                    try:
                        supports_delta = ("on_delta" in
                                          inspect.signature(model_insights).parameters)
                    except (TypeError, ValueError):
                        supports_delta = isinstance(self.api_analyzer, NodeAnalysis)
                    self._reserve_api_request(job,scope,store)
                    if supports_delta:
                        response = model_insights(
                            config["protocol"], config["baseUrl"], api_key, config["model"],
                            wire, target_ids, on_delta=on_delta)
                    else:
                        response = model_insights(
                            config["protocol"], config["baseUrl"], api_key, config["model"],
                            wire, target_ids)
                    job.setdefault("timings", {})["providerMs"] = round(
                        (time.perf_counter() - provider_started) * 1000, 3)
                    response_timings = response.get("timings") if isinstance(response, dict) else None
                    if isinstance(response_timings, dict):
                        for key in ("firstBodyMs", "connectorMs", "parseMs"):
                            value = response_timings.get(key)
                            if isinstance(value, (int, float)) and math.isfinite(value):
                                job["timings"][key] = round(float(value), 3)
                    insights = response["insights"]
                    if (len(insights) != len(pending) or
                            {item.get("id") for item in insights if isinstance(item, dict)} != set(target_ids)):
                        raise RuntimeError("invalid-insights")
                    # Validate the new affect/intents shape (with legacy scalar ok
                    # compat) and store only the normalized result.
                    validate_started = time.perf_counter()
                    by_id = {}
                    for item in insights:
                        try:
                            normalized = normalize_api_insight(item)
                        except ValueError:
                            raise RuntimeError("invalid-insights")
                        by_id[normalized["id"]] = normalized
                    job["timings"]["validateMs"] = round(
                        (time.perf_counter() - validate_started) * 1000, 3)
                except Exception as exc:
                    code = str(exc)
                    if attempt >= API_INSIGHT_RETRY_MAX or code not in API_INSIGHT_RETRYABLE:
                        raise
                    self._assert_scope(scope)
                    self._wait_api_model_retry(self.api_jobs, job_key, job, store,
                                               code, attempt + 1,
                                               retry_seconds=API_INSIGHT_RETRY_SECONDS,
                                               retry_max=API_INSIGHT_RETRY_MAX)
                    continue
                break
            with self.api_lock:
                if (self.closing or self.active_model_source_mode != "api" or
                        self.active_model_source_id != job_key[2] or
                        self.api_jobs.get(job_key) is not job or
                        store.cache_suspended(job_key[0], job_key[2])):
                    raise RuntimeError("model-source-changed")
                source_lock = getattr(self.source, "lock", None)
                with source_lock if source_lock is not None else nullcontext():
                    self._assert_scope(scope)
                    save_started = time.perf_counter()
                    store.save_api_insights(job_key[0], job_key[1], api_insight_scope(job_key[2]),
                                            [(message, by_id[message["id"]]) for message in pending])
                    self._publish_api_analysis(store)
                    job.setdefault("timings", {})["saveMs"] = round(
                        (time.perf_counter() - save_started) * 1000, 3)
                job["processed"] = job["total"]
                # prepareMs covers method entry -> job creation; add the wall clock since
                # creation so the total still starts before the job existed.
                job["timings"]["totalMs"] = max(0.0, round(
                    job["timings"].get("prepareMs", 0.0) +
                    time.time() * 1000 - job["startedAtMs"], 3))
                job["status"] = "done"
        except AnalysisInterrupted as exc:
            with self.api_lock:
                job.pop("retry",None)
                job["status"]=exc.state
        except Exception as exc:
            code = str(exc)
            with self.api_lock:
                job.pop("retry", None)
                job["error"] = code if code in MODEL_CONNECTOR_ERRORS or code=="analysis-budget-exhausted" or code in {
                    "invalid-insights", "model-source-changed"} else "model-analysis-failed"
                job["status"] = "error"
        finally:
            self.api_tasks.finish()

    def _api_portrait_history(self, user, subject, highwater, scope, after=None,
                              piece_limit_bytes=None, cancel_check=None, start_after=None,
                              skip_first_pieces=0, partial_sort=None):
        """Read a frozen range and retain pieces plus compact per-row seek evidence."""
        full_digest, delta_digest = hashlib.sha256(), hashlib.sha256()
        pieces = []
        retained_bytes = 0
        full = {"messageCount": 0, "textCount": 0, "targetTextCount": 0,
                "totalChars": 0, "pieceCount": 0}
        delta = dict(full)
        row_hashes = bytearray()
        cursor = start_after
        previous_sort = start_after
        first_item = True

        while highwater is not None:
            self._assert_scope(scope)
            if cancel_check is not None:
                cancel_check()
            page, next_cursor = self.source.history_page(user, highwater, cursor, page_size=1000)
            self._assert_scope(scope)
            if cancel_check is not None:
                cancel_check()
            if next_cursor is None:
                break
            if cursor is not None and next_cursor <= cursor:
                raise RuntimeError("history cursor did not advance")
            for item in page:
                item_sort = tuple(item["_sort"])
                current = after is None or item_sort > after
                full["messageCount"] += 1
                if current:
                    delta["messageCount"] += 1
                text = item.get("text")
                sender = item.get("side")
                evidence = json.dumps([item.get("id"), sender, item.get("senderId"),
                                       item.get("kind"), text if isinstance(text, str) else None],
                                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                full_digest.update(evidence)
                row_index = len(row_hashes) // 32 if row_hashes is not None else -1
                if row_hashes is not None:
                    if piece_limit_bytes is not None and retained_bytes + 32 > piece_limit_bytes:
                        pieces, row_hashes = None, None
                    else:
                        row_hashes.extend(hashlib.sha256(evidence).digest())
                        retained_bytes += 32
                if current:
                    delta_digest.update(evidence)
                if first_item and skip_first_pieces and item_sort != partial_sort:
                    raise ValueError("unfinished portrait source changed")
                if item.get("kind") != "text" or sender not in ("self", "other") or not isinstance(text, str) or not text:
                    if first_item and skip_first_pieces:
                        raise ValueError("unfinished portrait source changed")
                    previous_sort, first_item = item_sort, False
                    continue
                if (first_item and skip_first_pieces >=
                        (len(text) + API_PORTRAIT_PIECE_CHARS - 1) // API_PORTRAIT_PIECE_CHARS):
                    raise ValueError("unfinished portrait source changed")
                target = matches_target(item, '' if subject==user and source_is_group(self.source,user) else subject,
                                        is_group=source_is_group(self.source,user)) and (self.source_kind=='qq' or sender=='other')
                for counts in (full, delta) if current else (full,):
                    counts["textCount"] += 1
                    counts["targetTextCount"] += int(target)
                    counts["totalChars"] += len(text)
                for offset in range(0, len(text), API_PORTRAIT_PIECE_CHARS):
                    piece_index = offset // API_PORTRAIT_PIECE_CHARS
                    piece = {"id": f"{item['id']}:{offset // API_PORTRAIT_PIECE_CHARS}",
                             "sender": "SELF" if sender == "self" else "OTHER",
                             "target": target, "text": text[offset:offset + API_PORTRAIT_PIECE_CHARS],
                             "_last": offset + API_PORTRAIT_PIECE_CHARS >= len(text),
                             "_sort": item_sort, "_before": previous_sort,
                             "_pieceIndex": piece_index, "_rowIndex": row_index}
                    if self.source_kind=='qq':
                        piece['conversationKind']='group' if source_is_group(self.source,user) else 'friend'
                        if piece['conversationKind']=='group':
                            from qq_roles import group_context
                            piece['groupContext']=group_context(item)
                    full["pieceCount"] += 1
                    if current:
                        delta["pieceCount"] += 1
                        if first_item and piece_index < skip_first_pieces:
                            continue
                        if pieces is not None:
                            retained_bytes += len(piece["text"].encode("utf-8"))
                            if piece_limit_bytes is not None and retained_bytes > piece_limit_bytes:
                                pieces, row_hashes = None, None
                            else:
                                pieces.append(piece)
                previous_sort, first_item = item_sort, False
            cursor = next_cursor
        self._assert_scope(scope)
        if cancel_check is not None:
            cancel_check()
        if skip_first_pieces and first_item:
            raise ValueError("unfinished portrait source changed")
        def available(counts):
            return {key: counts[key] for key in
                    ("messageCount", "textCount", "targetTextCount", "totalChars", "pieceCount")}
        return (pieces, available(delta), delta_digest.hexdigest(),
                available(full), full_digest.hexdigest(), row_hashes)

    def _run_api_portrait_inventory(self, key, scope, store):
        account, _workdir, user, subject, highwater = key[:5]
        try:
            request_scope = getattr(self.source, "request_scope", None)
            with request_scope() if callable(request_scope) else nullcontext():
                pieces, _delta, _delta_fingerprint, available, fingerprint, _rows = (
                    self._api_portrait_history(user, subject, highwater, scope,
                                               piece_limit_bytes=API_PORTRAIT_INVENTORY_CACHE_BYTES))
                self._assert_scope(scope)
            with self.api_condition:
                if not self.closing:
                    store.api_history_inventory_save(account, user, subject, highwater,
                                                     fingerprint, available)
                    self._publish_api_analysis(store)
                    if pieces is not None and _rows is not None:
                        self.api_portrait_inventory_pieces = (key, pieces, available,
                                                              fingerprint, time.monotonic() + 60,
                                                              _rows)
                self.api_tasks.finish_inventory(key)
        except Exception:
            self.api_tasks.error_inventory(key, time.monotonic() + 10)
        finally:
            self.api_tasks.finish()

    @api_published_read
    def model_portrait(self, user, member=None):
        account, workdir, store = self._scoped_identity()
        if self.source_kind=='qq' and source_is_group(self.source,user) and member is None:
            overview=self._group_overview(account,user)
            return {'account':account,'sourceId':self.active_model_source_id,'subject':user,
                'identity':{'username':user,**self.source.contact(user),'isGroup':True,'members':overview['members'],
                    'messageCount':overview['stats']['messageCount'],'textCount':overview['stats']['textCount']},
                'overview':overview,'objectiveOverview':True,'portrait':None,'inventoryReady':True,'inventoryStatus':'objective',
                'available':{'messageCount':overview['stats']['messageCount'],'textCount':overview['stats']['textCount'],
                    'targetTextCount':0,'totalChars':None,'pieceCount':0},
                'progress':{'processed':0,'total':0,'processedTargetTexts':0,'totalTargetTexts':0,'batchIndex':0,'batchTotal':0,'complete':True},
                'suspended':False,'job':{'status':'done'}}
        with self.api_condition:
            snapshot = self.api_portrait_inventory_pieces
            if snapshot is not None and (snapshot[4] < time.monotonic() or
                                         snapshot[0][:2] != (account, workdir)):
                self.api_portrait_inventory_pieces = None
        if member is not None and (not isinstance(member, str) or not member or len(member) > 256):
            raise ValueError("invalid member")
        subject = target_subject(self.source,user,member) or user
        group = source_is_group(self.source,user)
        repository = self._qq_api_store(account, workdir, store)
        inventory_store = repository.bind(user, "inventory", "inventory", subject) if repository else store
        inventory_scope = inventory_store.scope if repository else (account, workdir)
        highwater = self.source.history_highwater(user)
        message_count = text_count = None
        # Group reads must stay cheap: per-message text classification is reserved for
        # the portrait inventory, and its persisted totals are reused here.
        if not group and member is None and self.source_kind!='qq' and hasattr(self.source, "contact"):
            contact, members = self.source.contact(user), []
        elif hasattr(self.source, "profile_overview"):
            overview = self.source.profile_overview(user, member, highwater)
            contact, members = overview["contact"], overview["members"]
            message_count = overview["count"]
        elif hasattr(self.source, "profile_metadata"):
            metadata = self.source.profile_metadata(user, member)
            contact, members = metadata["contact"], metadata["members"]
            message_count, text_count = metadata["count"], metadata["textCount"]
        else:
            message_count, text_count, members = self.source.stats(user)
            if member and member not in {item["id"] for item in members}:
                raise ValueError("unknown member")
            if member:
                message_count, text_count = self.source.stats(user, member)[:2]
            contact = self.source.contact(subject)
        self._assert_scope((account, workdir))
        with self.api_lock:
            mode, source_id = self.active_model_source_mode, self.active_model_source_id
            revision = repository.status(user, source_id, "portrait", subject) if repository and mode == "api" else {}
            running = self.api_portrait_jobs.get((account, user, source_id, subject))
            if revision and running and running.get("dataRevision") != revision["dataRevision"]:
                running = None
            if (running and running.get("status") == "error" and
                    running.get("_contextTokens") !=
                    (self.active_api_config or {}).get("contextTokens")):
                del self.api_portrait_jobs[(account, user, source_id, subject)]
                running = None
            job = {key: value for key, value in running.items() if not key.startswith("_")} if running else {
                "id": None, "status": "idle", "processed": 0, "total": 0}
        saved = (store.api_portrait_get(account, user, api_portrait_scope(source_id), subject)
                 if mode == "api" else None)
        published_saved = saved
        if repository and mode == "api" and saved is None and not store.cache_suspended(account, source_id):
            # With no earlier successful result, a current-revision draft can show
            # useful progress. It never replaces a published portrait mid-rebuild.
            draft = repository.bind(user, source_id, "portrait", subject)
            saved = draft.api_portrait_get(account, user, api_portrait_scope(source_id), subject)
        if running and running["status"] in ("queued", "running") and saved and running.get("_available") and not revision.get("stale"):
            highwater, available = saved["highwater"], running["_available"]
            inventory_status = "ready"
        elif saved is not None and not revision.get("stale"):
            # Render the saved portrait from its persisted inventory immediately after
            # a reload. A fresh full-history scan is deferred to the portrait POST and
            # must never make this read wait seconds behind unrelated inventory work.
            available = saved["available"].get("fullAvailable")
            if not isinstance(available, dict) or not all(
                    type(available.get(key)) is int for key in API_PORTRAIT_COUNT_KEYS):
                inventory = inventory_store.api_history_inventory_get(account, user, subject,
                                                            saved["highwater"])
                if inventory is not None:
                    available = inventory["available"]
                else:
                    # Legacy delta rows may lack a full inventory. Text and target
                    # totals remain exact from their persisted base plus delta; the
                    # other inventory fields stay unknown until one migration scan.
                    delta = saved["available"]
                    available = {
                        "messageCount": None,
                        "textCount": delta.get("baseTextCount", 0) + delta["textCount"],
                        "targetTextCount": delta.get("baseTargetTextCount", 0) +
                                           delta["targetTextCount"],
                        "totalChars": None, "pieceCount": None,
                    }
            inventory_status = "ready"
        else:
            inventory = inventory_store.api_history_inventory_get(account, user, subject, highwater)
            if inventory is None:
                available = None
                key = self._inventory_key(account, workdir, user, subject, highwater, inventory_scope)
                # Reservation and inflight accounting are one critical section,
                # including thread-start rollback. Per-method locking alone races.
                with self.api_condition:
                    inventory_status = self.api_tasks.inventory_status(key, time.monotonic())
                    if inventory_status is None:
                        inventory_status = "running"
                        # Rapid session switching must not start unlimited history readers.
                        if not self.api_tasks.has_running_inventory():
                            self.api_tasks.register_inventory(key)
                            self.api_tasks.begin()
                            thread = threading.Thread(target=self._run_api_portrait_inventory,
                                                      args=(key, inventory_scope, inventory_store), daemon=True)
                            try:
                                thread.start()
                            except Exception:
                                self.api_tasks.finish()
                                self.api_tasks.error_inventory(key, time.monotonic() + 10)
                                inventory_status = "error"
            else:
                available = inventory["available"]
                inventory_status = "ready"
        self._assert_scope((account, workdir))
        if group and text_count is None and available is not None:
            text_count = available["targetTextCount"] if member else available["textCount"]
            if not member and available.get("messageCount") is not None:
                # Present counts from one indexed history snapshot, not a fresh
                # message total beside an older text denominator.
                message_count = available["messageCount"]
        identity = {"username": subject, **contact, "isGroup": group,
                    "members": members if group else [],
                    **({'targetSide':'self' if subject==SELF_SUBJECT else 'other','messageCount':message_count} if self.source_kind=='qq' and not group else {}),
                    **({"messageCount": message_count, "textCount": text_count} if group else {})}
        if saved:
            base = saved["available"].get("baseTextCount", 0)
            base_target = saved["available"].get("baseTargetTextCount", 0)
            processed_target = saved["available"].get("processedTargetTextCount")
            if type(processed_target) is not int:
                processed_target = math.floor(saved["available"].get("targetTextCount", 0) *
                                              saved["processed"] /
                                              max(1, saved["available"].get("textCount", 0)))
            up_to_date = saved["highwater"] == highwater and not revision.get("stale")
            progress = {"processed": base + saved["processed"],
                        "total": available["textCount"] if up_to_date and available else max(
                            available["textCount"] if available else 0,
                            base + saved["available"]["textCount"]),
                        "processedTargetTexts": base_target + processed_target,
                        "totalTargetTexts": available["targetTextCount"] if available else
                                            base_target + saved["available"]["targetTextCount"],
                        "batchIndex": saved["batchIndex"], "batchTotal": len(saved["plan"]),
                        "complete": saved["complete"] and up_to_date}
        else:
            progress = {"processed": 0, "total": available["textCount"] if available else 0,
                        "processedTargetTexts": 0,
                        "totalTargetTexts": available["targetTextCount"] if available else 0,
                        "batchIndex": 0, "batchTotal": 0, "complete": False}
        published_progress = dict(progress)
        if repository and running and running.get("status") in ("queued", "running"):
            progress = {**progress, **{name: running[name] for name in (
                "processed", "total", "processedTargetTexts", "totalTargetTexts", "batchIndex", "batchTotal")
                if name in running}, "complete": False}
        return {"account": account, "sourceId": source_id, "subject": subject,
                "identity": identity,
                "portrait": saved["portrait"] if saved else None,
                "available": available, "inventoryReady": inventory_status == "ready",
                "inventoryStatus": inventory_status, "progress": progress,
                "suspended": store.cache_suspended(account, source_id) if mode == "api" else False,
                "job": job, **revision,
                **({"hasPublishedAnalysis": published_saved is not None,
                    "publishedProgress": published_progress} if repository else {})}

    def analysis_cache_status(self):
        account, workdir, store = self._scoped_identity()
        sources = store.analysis_cache_sources(account)
        with self.api_lock:
            if self.active_model_source_mode == "api" and self.active_api_config:
                source_id = self.active_model_source_id
                current = next((item for item in sources if item["sourceId"] == source_id), None)
                if current is None:
                    sources.append({"sourceId": source_id, "kind": "api",
                                    "label": self.active_api_config["model"],
                                    "protocol": self.active_api_config["protocol"],
                                    "messageCount": 0, "portraitCount": 0,
                                    "suspended": store.cache_suspended(account, source_id)})
                else:
                    current["label"] = self.active_api_config["model"]
                    current["protocol"] = self.active_api_config["protocol"]
        self._assert_scope((account, workdir))
        return {"account": account, "sources": sources}

    def analysis_cache_clear(self, requested_account, source_id):
        account, workdir, store = self._scoped_identity()
        if account != requested_account or not isinstance(source_id, str):
            raise AccountChangedError()
        if source_id not in {item["sourceId"] for item in self.analysis_cache_status()["sources"]}:
            raise ValueError("unknown analysis source")
        clear_scope = (account, source_id)
        with self.request_condition:
            if clear_scope in self.cache_clear_in_progress:
                raise RuntimeError("cache clear already running")
            self.cache_clear_in_progress.add(clear_scope)
        was_suspended = False
        quiesced = False
        try:
            was_suspended = store.cache_suspended(account, source_id)
            store.suspend_cache(account, source_id)
            quiesced = True
            if source_id == LOCAL_SOURCE_ID:
                with self.tasks.all_tasks_done:
                    if not self.tasks.all_tasks_done.wait_for(
                            lambda: self.tasks.unfinished_tasks == 0, timeout=200):
                        raise RuntimeError("local analysis worker did not drain")
                # No worker turn can now write the selected account. Drop paused
                # iterators so a later analysis starts from empty saved state.
                with self.jobs_lock:
                    for key in list(self.jobs):
                        if key[0] == account:
                            del self.jobs[key]
                    for mapping in (self.priority_recent, self.recent_windows):
                        for key in list(mapping):
                            if key[0] == account:
                                del mapping[key]
                    self.incremental_recheck = {key for key in self.incremental_recheck
                                                if key[0] != account}
                for key, iterator in list(self.history_iterators.items()):
                    if key[0] == account:
                        iterator.close()
                        del self.history_iterators[key]
                        self.history_iterator_modes.pop(key, None)
                for key, iterator in list(self.quoted_backfill_iterators.items()):
                    if key[0] == account:
                        iterator.close()
                        del self.quoted_backfill_iterators[key]
                if self.batch_engine:
                    for mapping in (self.batch_engine.member_jobs, self.batch_engine.member_iterators):
                        for key in list(mapping):
                            if key[0] == account:
                                if mapping is self.batch_engine.member_iterators:
                                    mapping[key].close()
                                del mapping[key]
                for key in list(self.performance):
                    if key[0] == account:
                        del self.performance[key]
            else:
                # Source-scoped: only drop this source's jobs. The suspension above
                # makes its writers refuse to save, so unrelated inflight inventory or
                # other sources must not be waited on here.
                with self.api_condition:
                    snapshot = self.api_tasks.drop_inventory_pieces_for(account)
                    self.api_tasks.invalidate_source(account, source_id)
            self._assert_scope((account, workdir))
            if self.source_kind == "qq" and source_id != LOCAL_SOURCE_ID:
                with self.api_lock, self.source.read_library(account):
                    store.clear_analysis_cache(account, source_id)
                    repository = self._qq_api_store(account, workdir, store)
                    repository.clear_source(source_id)
            else:
                store.clear_analysis_cache(account, source_id)
            if source_id == LOCAL_SOURCE_ID:
                for (owner, _path), repository in self.qq_analysis_stores.items():
                    if owner == account:
                        repository.clear_stages()
                # Every source owns this public entry (§2.0/§2.3); reaching into a private
                # cache attribute would leave a source that hides one silently stale.
                self.source.invalidate_profile_metadata()
            # Clear deletes the saved analysis and portraits, so the source must be
            # usable immediately rather than hidden behind a separate Restore action.
            # Lifting the quiesce flag inside the critical section avoids a window
            # where a fresh request sees the source as suspended.
            store.resume_cache(account, source_id)
        except Exception:
            # Only undo the transient quiesce flag this call actually added, so a
            # source that was already suspended (or whose state could not be read)
            # keeps its prior state.
            if quiesced and not was_suspended:
                store.resume_cache(account, source_id)
            raise
        finally:
            with self.request_condition:
                self.cache_clear_in_progress.discard(clear_scope)
                self.request_condition.notify_all()
        return {"cleared": True, "account": account, "sourceId": source_id}

    def analysis_cache_resume(self, requested_account, source_id):
        account, workdir, store = self._scoped_identity()
        if account != requested_account or not isinstance(source_id, str):
            raise AccountChangedError()
        if source_id not in {item["sourceId"] for item in self.analysis_cache_status()["sources"]}:
            raise ValueError("unknown analysis source")
        with self.api_lock:
            store.resume_cache(account, source_id)
        self._assert_scope((account, workdir))
        return {"resumed": True, "account": account, "sourceId": source_id}

    @api_published_read
    def start_model_portrait(self, requested_account, user, member=None, refresh_axes=False):
        if member is not None and (not isinstance(member, str) or not member or len(member) > 256):
            raise ValueError("invalid member")
        if type(refresh_axes) is not bool:
            raise ValueError("invalid portrait refresh")
        account, workdir, store = self._scoped_identity()
        if account != requested_account:
            raise AccountChangedError()
        if self.source_kind=="qq":require_running(store,account,user)
        if self.source_kind=='qq' and source_is_group(self.source,user) and member is None:
            raise ValueError('group-portrait-target-required')
        subject = target_subject(self.source,user,member) or user
        with self.api_lock:
            if self.active_model_source_mode != "api" or not self.active_api_config:
                raise ModelSourceUnavailable("API model is not active")
            source_id = self.active_model_source_id
            config = dict(self.active_api_config)
            api_key = self.model_source_store.resolve_key(config["protocol"], config["baseUrl"], None)
            if store.cache_suspended(account, source_id):
                return {"account": account, "sourceId": source_id,
                        "job": {"id": None, "status": "suspended", "processed": 0}}
            repository = self._qq_api_store(account, workdir, store)
            source_scope = (account, workdir)
            if repository:
                store = repository.bind(user, source_id, "portrait", subject)
                source_scope = store.scope
            job_key = (account, user, source_id, subject)
            current = self.api_portrait_jobs.get(job_key)
            if (current and current["status"] in ("queued", "running") and
                    (not repository or current.get("dataRevision") == store.revision)):
                return {"account": account, "sourceId": source_id,
                        "job": {key: value for key, value in current.items() if not key.startswith("_")}}
            if refresh_axes:
                # Re-estimate MBTI/traits from the saved cumulative portrait with one
                # bounded call: no history rescan, no progress reset, same source scope.
                saved = store.api_portrait_get(account, user, api_portrait_scope(source_id), subject)
                if saved is None:
                    raise ValueError("no saved portrait to re-evaluate")
                if repository and not saved["complete"]:
                    raise ValueError("finish-portrait-before-axes-refresh")
                job = {"id": uuid.uuid4().hex, "status": "queued",
                       "processed": saved["available"].get("baseTextCount", 0) + saved["processed"],
                       "total": saved["available"].get("baseTextCount", 0) + saved["available"]["textCount"],
                       "batchIndex": saved["batchIndex"], "batchTotal": len(saved["plan"]),
                       "startedAtMs": int(time.time() * 1000),
                       "_axesRefresh": True, "_contextTokens": config.get("contextTokens"),
                       **({"dataRevision": store.revision} if repository else {})}
                self.api_portrait_jobs[job_key] = job
                self.api_tasks.begin()
                thread = threading.Thread(target=self._run_model_portrait_axes,
                                          args=(job_key, job, source_scope, store, config,
                                                api_key, saved), daemon=True)
                try:
                    thread.start()
                except Exception:
                    self.api_tasks.finish()
                    job.update(status="error", error="portrait-analysis-failed")
                    raise
                return {"account": account, "sourceId": source_id,
                        "job": {key: value for key, value in job.items() if not key.startswith("_")}}
            # The full history scan and batch planning can take minutes. Register
            # the scoped job before starting that work so POST can return at once.
            job = {"id": uuid.uuid4().hex, "status": "queued", "phase": "preparing",
                   "processed": 0, "total": 0, "processedTargetTexts": 0,
                   "totalTargetTexts": 0, "batchIndex": 0, "batchTotal": 0,
                   "startedAtMs": int(time.time() * 1000),
                   "_contextTokens": config.get("contextTokens"),
                   **({"dataRevision": store.revision} if repository else {})}
            self.api_portrait_jobs[job_key] = job
            self.api_tasks.begin()
            thread = threading.Thread(target=self._run_model_portrait,
                                      args=(job_key, job, source_scope, store, config, api_key),
                                      daemon=True)
            try:
                thread.start()
            except Exception:
                self.api_tasks.finish()
                job.update(status="error", error="portrait-analysis-failed")
                raise
            return {"account": account, "sourceId": source_id,
                    "job": {key: value for key, value in job.items() if not key.startswith("_")}}

    def _assert_api_portrait_job(self, job_key, job, store, config):
        """A queued scan must not outlive its exact source, budget, or cache scope."""
        with self.api_lock:
            if job.get("status") in ("paused","cancelled"):
                raise AnalysisInterrupted(job["status"])
            if (self.closing or self.active_model_source_mode != "api" or
                    self.active_model_source_id != job_key[2] or
                    (self.active_api_config or {}).get("contextTokens") != config.get("contextTokens") or
                    self.api_portrait_jobs.get(job_key) is not job or
                    store.cache_suspended(job_key[0], job_key[2])):
                raise RuntimeError("model-source-changed")

    def _prepare_model_portrait(self, job_key, job, scope, store, config):
        """Freeze and plan history in the background; only then begin the cursor."""
        account, user, source_id, subject = job_key
        self._assert_scope(scope)
        self._assert_api_portrait_job(job_key, job, store, config)
        existing = store.api_portrait_get(account, user, api_portrait_scope(source_id), subject)
        current_highwater = self.source.history_highwater(user)
        self._assert_scope(scope)
        self._assert_api_portrait_job(job_key, job, store, config)
        if existing and existing["complete"] and existing["highwater"] is not None and (
                current_highwater is None or current_highwater < existing["highwater"]):
            raise ValueError("unfinished portrait source changed")
        if existing and existing["complete"] and existing["highwater"] == current_highwater:
            with self.api_lock:
                self._assert_scope(scope)
                self._assert_api_portrait_job(job_key, job, store, config)
                self._publish_api_analysis(store)
                job.update(status="done",
                           processed=existing["available"].get("baseTextCount", 0) + existing["processed"],
                           total=existing["available"].get("baseTextCount", 0) +
                                 existing["available"]["textCount"],
                           batchIndex=existing["batchIndex"], batchTotal=len(existing["plan"]))
                job.pop("phase", None)
            return None
        highwater = existing["highwater"] if existing and not existing["complete"] else current_highwater
        after = existing["after"] if existing and not existing["complete"] else (
            existing["highwater"] if existing else None)
        base_count = (existing["available"].get("baseTextCount", 0) +
                      existing["available"]["textCount"] if existing and existing["complete"] else
                      existing["available"].get("baseTextCount", 0) if existing else 0)
        base_target_count = (existing["available"].get("baseTargetTextCount", 0) +
                             existing["available"]["targetTextCount"] if existing and existing["complete"] else
                             existing["available"].get("baseTargetTextCount", 0) if existing else 0)
        saved_full = existing["available"].get("fullAvailable") if existing else None
        saved_full_fingerprint = existing["available"].get("fullFingerprint") if existing else None
        if existing and (not isinstance(saved_full, dict) or
                         not isinstance(saved_full_fingerprint, str)):
            inventory = store.api_history_inventory_get(account, user, subject,
                                                        existing["highwater"])
            if inventory is not None:
                saved_full, saved_full_fingerprint = inventory["available"], inventory["fingerprint"]
        has_full = (isinstance(saved_full, dict) and isinstance(saved_full_fingerprint, str) and
                    bool(saved_full_fingerprint) and
                    all(type(saved_full.get(key)) is int and saved_full[key] >= 0
                        for key in API_PORTRAIT_COUNT_KEYS))
        completed = existing["batchIndex"] if existing and not existing["complete"] else 0
        piece_offset = existing["plan"][completed - 1] if completed else 0
        resume = existing.get("resume") if existing and not existing["complete"] else None
        valid_resume = (isinstance(resume, dict) and resume.get("batchIndex") == completed and
                        resume.get("pieceOffset") == piece_offset and
                        isinstance(resume.get("tailHash"), str) and
                        len(resume["tailHash"]) == 64 and
                        type(resume.get("skipPieces")) is int and resume["skipPieces"] >= 0 and
                        (resume.get("after") is None or
                         isinstance(resume["after"], list) and len(resume["after"]) == 3) and
                        (resume.get("partialSort") is None or
                         isinstance(resume["partialSort"], list) and len(resume["partialSort"]) == 3) and
                        (resume["skipPieces"] == 0) == (resume.get("partialSort") is None))
        fast_resume = bool(existing and not existing["complete"] and has_full and
                           (completed == 0 or valid_resume))
        fast_incremental = bool(existing and existing["complete"] and has_full)
        if fast_resume and completed:
            start_after = tuple(resume["after"]) if resume["after"] is not None else None
        elif fast_resume or fast_incremental:
            start_after = after
        else:
            start_after = None
        skip_pieces = resume["skipPieces"] if fast_resume and completed else 0
        partial_sort = (tuple(resume["partialSort"]) if skip_pieces and
                        resume["partialSort"] is not None else None)
        snapshot = None
        pieces = None
        row_hashes = None
        if start_after is None and after is None:
            snapshot_key = self._inventory_key(account, scope[1], user, subject, highwater, scope)
            deadline = time.monotonic() + 30
            while True:
                self._assert_scope(scope)
                self._assert_api_portrait_job(job_key, job, store, config)
                with self.api_condition:
                    if self.api_portrait_inventory_jobs.get(snapshot_key) != "running":
                        snapshot = self.api_portrait_inventory_pieces
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        snapshot = self.api_portrait_inventory_pieces
                        break
                    self.api_condition.wait(timeout=min(1, remaining))
            if (snapshot is not None and snapshot[0] == snapshot_key and
                    snapshot[4] >= time.monotonic() and len(snapshot) > 5):
                pieces, full_available, fingerprint = snapshot[1:4]
                available, full_fingerprint, row_hashes = dict(full_available), fingerprint, snapshot[5]
        if pieces is None:
            pieces, scan_available, scan_fingerprint, scan_full, scan_full_fingerprint, row_hashes = (
                self._api_portrait_history(
                    user, subject, highwater, scope, after,
                    cancel_check=lambda: self._assert_api_portrait_job(job_key, job, store, config),
                    start_after=start_after, skip_first_pieces=skip_pieces,
                    partial_sort=partial_sort))
            if fast_resume:
                tails = api_portrait_tail_hashes(row_hashes)
                if completed and tails[:32].hex() != resume["tailHash"]:
                    raise ValueError("unfinished portrait source changed")
                if not completed and (scan_fingerprint != existing["fingerprint"] or
                                      any(scan_available[key] != existing["available"][key]
                                          for key in API_PORTRAIT_COUNT_KEYS)):
                    raise ValueError("unfinished portrait source changed")
                available = dict(existing["available"])
                fingerprint = existing["fingerprint"]
                full_available, full_fingerprint = saved_full, saved_full_fingerprint
            elif fast_incremental:
                available, fingerprint = scan_available, scan_fingerprint
                full_available = api_portrait_add_counts(saved_full, scan_full)
                full_fingerprint = "chain-v2:" + hashlib.sha256(
                    (saved_full_fingerprint + ":" + scan_fingerprint).encode("ascii")).hexdigest()
            else:
                available, fingerprint = scan_available, scan_fingerprint
                full_available, full_fingerprint = scan_full, scan_full_fingerprint
                if existing and not existing["complete"] and fingerprint != existing["fingerprint"]:
                    raise ValueError("unfinished portrait source changed")
        self._assert_scope(scope)
        self._assert_api_portrait_job(job_key, job, store, config)
        tails = api_portrait_tail_hashes(row_hashes)
        legacy_resume = None
        if existing and not existing["complete"] and completed and not fast_resume:
            if piece_offset < 1 or piece_offset > len(pieces):
                raise ValueError("unfinished portrait source changed")
            legacy_resume = api_portrait_resume_anchor(
                pieces[piece_offset - 1], piece_offset, completed, tails)
            if type(existing["available"].get("processedTargetTextCount")) is not int:
                available["processedTargetTextCount"] = sum(
                    bool(item["target"] and item["_last"]) for item in pieces[:piece_offset])
            pieces = pieces[piece_offset:]
        if existing and not existing["complete"] and not pieces:
            raise ValueError("unfinished portrait source changed")
        available["baseTextCount"] = base_count
        available["baseTargetTextCount"] = base_target_count
        available["fullAvailable"] = full_available
        available["fullFingerprint"] = full_fingerprint
        wire_chars = api_portrait_wire_chars(config.get("contextTokens"))
        if existing and not existing["complete"]:
            prefix = existing["plan"][:completed]
            plan = prefix + [piece_offset + end for end in api_portrait_plan(pieces, wire_chars)]
        else:
            plan = api_portrait_plan(pieces, wire_chars)
        self._assert_scope(scope)
        source_lock = getattr(self.source, "lock", None)
        with self.api_lock:
            self._assert_api_portrait_job(job_key, job, store, config)
            with source_lock if source_lock is not None else nullcontext():
                self._assert_scope(scope)
                store.api_history_inventory_save(account, user, subject, highwater,
                                                 full_fingerprint, full_available)
                store.register_api_source(account, source_id, config["protocol"], config["model"])
                saved = store.api_portrait_begin(account, user, api_portrait_scope(source_id), subject,
                                                 highwater, after, fingerprint, available, plan)
                if existing and not existing["complete"] and (
                        available != saved["available"] or legacy_resume is not None):
                    store.api_portrait_upgrade_resume(
                        account, user, api_portrait_scope(source_id), subject,
                        fingerprint, completed, available, legacy_resume or saved["resume"])
                    saved = store.api_portrait_get(account, user, api_portrait_scope(source_id), subject)
                if (snapshot is not None and pieces is snapshot[1] and
                        self.api_portrait_inventory_pieces is snapshot):
                    self.api_portrait_inventory_pieces = None
                job.update(processed=base_count + saved["processed"],
                           total=base_count + available["textCount"],
                           processedTargetTexts=base_target_count +
                                                saved["available"].get("processedTargetTextCount", 0),
                           totalTargetTexts=base_target_count + available["targetTextCount"],
                           batchIndex=saved["batchIndex"], batchTotal=len(saved["plan"]),
                           _available=full_available, _contextTokens=config.get("contextTokens"))
                job.pop("phase", None)
                if saved["complete"]:
                    self._publish_api_analysis(store)
                    job["status"] = "done"
                    return None
                job["status"] = "running"
        return saved, pieces, piece_offset, tails

    def _wait_api_model_retry(self, jobs, job_key, job, store, code, attempt,
                              retry_seconds=None, retry_max=None):
        """Expose one retryable error and wait without holding the API lock.

        The per-call ``check`` preserves the source/closing/registry-identity/suspension
        checks; the retry window and cap are read per call so tests can patch them.
        """
        retry_seconds = API_MODEL_RETRY_SECONDS if retry_seconds is None else retry_seconds
        retry_max = API_MODEL_RETRY_MAX if retry_max is None else retry_max

        def check():
            if job.get("status") in ("paused","cancelled"):
                raise AnalysisInterrupted(job["status"])
            if (self.closing or self.active_model_source_mode != "api" or
                    self.active_model_source_id != job_key[2] or
                    jobs.get(job_key) is not job or
                    store.cache_suspended(job_key[0], job_key[2])):
                raise RuntimeError("model-source-changed")
            if getattr(store, "component", None) in ("insights", "portrait"):
                self._assert_scope(store.scope)

        self.api_tasks.wait_api_model_retry(jobs, job_key, job, code, attempt,
                                            retry_seconds, retry_max, check)

    def _run_model_portrait(self, job_key, job, scope, store, config, api_key):
        """Keep the verified source reader pinned through generation and commit."""
        request_scope = getattr(self.source, "request_scope", None)
        try:
            with request_scope() if callable(request_scope) else nullcontext():
                self._run_model_portrait_scoped(job_key, job, scope, store, config, api_key)
        except AnalysisInterrupted as exc:
            with self.api_lock:
                job.pop("retry",None)
                job.pop("phase",None)
                job["status"]=exc.state
        except Exception as exc:
            code = str(exc)
            with self.api_lock:
                job.pop("retry", None)
                job.pop("phase", None)
                job.update(status="error", error=code if code in MODEL_CONNECTOR_ERRORS or
                           code in {"invalid-portrait", "model-source-changed"} else
                            "portrait-analysis-failed")
        finally:
            self.api_tasks.finish()

    def _run_model_portrait_scoped(self, job_key, job, scope, store, config, api_key):
        account, user, source_id, subject = job_key
        try:
            with self.api_lock:
                self._assert_api_portrait_job(job_key, job, store, config)
                job["status"] = "running"
            prepared = self._prepare_model_portrait(job_key, job, scope, store, config)
            if prepared is None:
                return
            saved, pieces, piece_offset, tails = prepared
            portrait = saved["portrait"]
            processed = saved["processed"]
            processed_chars = saved["processedChars"]
            plan = saved["plan"]
            processed_target = saved["available"].get("processedTargetTextCount")
            if type(processed_target) is not int:
                if saved["batchIndex"]:
                    raise RuntimeError("API portrait target checkpoint unavailable")
                processed_target = 0
            for batch_index in range(saved["batchIndex"], len(plan)):
                self._assert_scope(scope)
                with self.api_lock:
                    self._assert_api_portrait_job(job_key, job, store, config)
                start = (plan[batch_index - 1] if batch_index else 0) - piece_offset
                stop = plan[batch_index] - piece_offset
                batch = pieces[start:stop]
                if not batch:
                    raise RuntimeError("API portrait batch cursor unavailable")
                wire = [{key: item[key] for key in ("id", "sender", "target", "text", 'conversationKind', 'groupContext') if key in item}
                        for item in batch]
                batch_started = time.monotonic()
                job["batchStartedAtMs"] = int(time.time() * 1000)
                for attempt in range(API_MODEL_RETRY_MAX + 1):
                    self._assert_scope(scope)
                    self._assert_api_portrait_job(job_key, job, store, config)
                    try:
                        self._reserve_api_request(job,scope,store)
                        updated = self.api_portrait_analyzer.model_portrait(
                            config["protocol"], config["baseUrl"], api_key, config["model"], portrait, wire)
                        if not valid_api_portrait(updated):
                            raise RuntimeError("invalid-portrait")
                        if self.source_kind=='qq' and (source_is_group(self.source,user) or subject==SELF_SUBJECT):
                            updated['affinity']=None
                    except Exception as exc:
                        code = str(exc)
                        if attempt >= API_MODEL_RETRY_MAX or code not in API_MODEL_RETRYABLE:
                            raise
                        self._assert_scope(scope)
                        self._wait_api_model_retry(self.api_portrait_jobs, job_key, job,
                                                   store, code, attempt + 1)
                        continue
                    break
                self._assert_scope(scope)
                with self.api_lock:
                    self._assert_api_portrait_job(job_key, job, store, config)
                    source_lock = getattr(self.source, "lock", None)
                    with source_lock if source_lock is not None else nullcontext():
                        self._assert_scope(scope)
                        portrait = updated
                        completed_texts = sum(bool(item["_last"]) for item in batch)
                        completed_targets = sum(bool(item["target"] and item["_last"])
                                                for item in batch)
                        processed += completed_texts
                        processed_target += completed_targets
                        processed_chars += sum(len(item["text"]) for item in batch)
                        store.api_portrait_checkpoint(account, user, api_portrait_scope(source_id),
                                                      subject, batch_index + 1, portrait, processed,
                                                      processed_chars, batch_index + 1 == len(plan),
                                                      processed_target=processed_target,
                                                      resume=api_portrait_resume_anchor(
                                                          batch[-1], plan[batch_index],
                                                          batch_index + 1, tails))
                        if batch_index + 1 == len(plan):
                            self._publish_api_analysis(store)
                        job["processed"] = saved["available"].get("baseTextCount", 0) + processed
                        job["processedTargetTexts"] = (saved["available"].get("baseTargetTextCount", 0) +
                                                       processed_target)
                        job["batchIndex"] = batch_index + 1
                        elapsed = max(time.monotonic() - batch_started, .001)
                        job["rateTextsPerSecond"] = round(completed_texts / elapsed, 2)
                        job.pop("retry", None)
            job["status"] = "done"
        except AnalysisInterrupted:
            raise
        except Exception as exc:
            code = str(exc)
            with self.api_lock:
                job.pop("retry", None)
                job.pop("phase", None)
                job.update(status="error", error=code if code in MODEL_CONNECTOR_ERRORS or
                           code in {"invalid-portrait", "model-source-changed"} else
                           "portrait-analysis-failed")
    def _run_model_portrait_axes(self, job_key, job, scope, store, config, api_key, saved):
        """Pin the same source before the paid axes call and its checkpoint."""
        request_scope = getattr(self.source, "request_scope", None)
        try:
            with request_scope() if callable(request_scope) else nullcontext():
                self._run_model_portrait_axes_scoped(
                    job_key, job, scope, store, config, api_key, saved)
        except AnalysisInterrupted as exc:
            with self.api_lock:
                job.pop('retry',None)
                job['status']=exc.state
        except Exception as exc:
            code = str(exc)
            with self.api_lock:
                job.pop("retry", None)
                job.update(status="error", error=code if code in MODEL_CONNECTOR_ERRORS or
                           code in {"invalid-portrait", "model-source-changed"} else
                            "portrait-analysis-failed")
        finally:
            self.api_tasks.finish()

    def _run_model_portrait_axes_scoped(self, job_key, job, scope, store, config, api_key, saved):
        account, user, source_id, subject = job_key
        try:
            self._assert_scope(scope)
            with self.api_lock:
                self._assert_api_portrait_job(job_key, job, store, config)
                job["status"] = "running"
            portrait = saved["portrait"]
            job["batchStartedAtMs"] = int(time.time() * 1000)
            for attempt in range(API_MODEL_RETRY_MAX + 1):
                self._assert_scope(scope)
                self._assert_api_portrait_job(job_key, job, store, config)
                try:
                    self._reserve_api_request(job,scope,store)
                    update = self.api_portrait_analyzer.refresh_portrait_axes(
                        config["protocol"], config["baseUrl"], api_key, config["model"], portrait)
                    if not isinstance(update, dict) or not {"mbtiAxes", "traits", "affinity"} <= update.keys():
                        raise RuntimeError("invalid-portrait")
                    merged = {**portrait, "mbtiAxes": update["mbtiAxes"],
                              "traits": update["traits"], "affinity": update["affinity"]}
                    if not valid_api_portrait(merged):
                        raise RuntimeError("invalid-portrait")
                except Exception as exc:
                    code = str(exc)
                    if attempt >= API_MODEL_RETRY_MAX or code not in API_MODEL_RETRYABLE:
                        raise
                    self._assert_scope(scope)
                    self._wait_api_model_retry(self.api_portrait_jobs, job_key, job,
                                               store, code, attempt + 1)
                    continue
                break
            self._assert_scope(scope)
            with self.api_lock:
                self._assert_api_portrait_job(job_key, job, store, config)
                source_lock = getattr(self.source, "lock", None)
                with source_lock if source_lock is not None else nullcontext():
                    self._assert_scope(scope)
                    store.api_portrait_checkpoint(account, user, api_portrait_scope(source_id), subject,
                                                  saved["batchIndex"], merged, saved["processed"],
                                                  saved["processedChars"], saved["complete"])
                    self._publish_api_analysis(store)
            job["status"] = "done"
        except AnalysisInterrupted:
            raise
        except Exception as exc:
            code = str(exc)
            with self.api_lock:
                job.pop("retry", None)
                job.update(status="error", error=code if code in MODEL_CONNECTOR_ERRORS or
                           code in {"invalid-portrait", "model-source-changed"} else
                           "portrait-analysis-failed")
    def messages(self, user, limit):
        account, workdir, _ = self._scoped_identity()
        messages = self.source.messages(user, limit)
        self._assert_scope((account, workdir))
        return {"messages": [{key: value for key, value in item.items() if not key.startswith("_")} for item in messages],
                "account": account, "total": None,
                **({"hasMoreBefore": messages.has_more_before}
                   if type(getattr(messages, "has_more_before", None)) is bool else {})}

    def message_windows(self, requested_account, users):
        ready = getattr(self.source, "require_messages_ready", None)
        if callable(ready):
            ready()
        windows = self.source.message_windows(users, 80, expected_account=requested_account)
        return {"account": requested_account, "windows": [
            {"user": user, "messages": [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in windows[user]],
             **({"hasMoreBefore": windows.has_more_before[user]}
                if user in getattr(windows, "has_more_before", {}) else {})}
            for user in users]}

    def _browse_history(self, account, user, **kwargs):
        browse = browse_qq_history if self.source_kind == "qq" else browse_history
        return browse(self.source, account, user, **kwargs)

    @published_analysis_read
    def history(self, requested_account, user, *, before=None, around=None, limit=80,member=None):
        account, workdir, store = self._scoped_identity()
        if requested_account != account:
            raise AccountChangedError()
        page = self._browse_history(account, user, before=before, around=around,
                              limit=limit, max_issued_images=MAX_ISSUED_IMAGES,
                              **({'member':member} if self.source_kind=='qq' else {}))
        version = self.analyzer.analysis_version()
        result_messages = ([item for item in page["messages"] if item["kind"] == "text" and item["text"].strip()]
                           if self.source_kind == "qq" else page["messages"])
        results = saved_history_results(store, account, user, version, result_messages)
        results.update(store.fine_view(account, user, version,
                                      [item["id"] for item in result_messages]))
        repository = self._qq_analysis_store(account, workdir, store)
        revision = repository.status(user, version) if repository else {}
        if self.source_kind=='qq' and source_is_group(self.source,user):
            from qq_group_analysis import history_results
            results=history_results(store,account,user,version,revision['dataRevision'],[item['id'] for item in result_messages],source=self.source)
            revision={**revision,'hasPublishedAnalysis':bool(results),'analysisRevision':revision['dataRevision'] if results else None,'stale':False}
        elif revision.get("stale"):
            results = {}
        self._assert_scope((account, workdir))
        return {"account": account, "user": user,
                "messages": [{key: value for key, value in item.items() if not key.startswith("_")}
                             for item in page["messages"]],
                "results": results, "hasMoreBefore": page["hasMoreBefore"],
                "hasMoreAfter": page["hasMoreAfter"], "nextCursor": page["nextCursor"],
                "oldestCursor": page["oldestCursor"], "newestCursor": page["newestCursor"],
                "focusId": page["focusId"], **revision}

    def history_search(self, requested_account, user, *, query=None, day=None,
                       before=None, limit=50,member=None):
        account, workdir, _store = self._scoped_identity()
        if requested_account != account:
            raise AccountChangedError()
        search = search_qq_history if self.source_kind == "qq" else search_history
        page = search(self.source, account, user, query=query, day=day,
                              before=before, limit=limit,
                              max_issued_images=MAX_ISSUED_IMAGES,
                              **({'member':member} if self.source_kind=='qq' else {}))
        self._assert_scope((account, workdir))
        return {"account": account, "user": user,
                "messages": [{key: value for key, value in item.items() if not key.startswith("_")}
                             for item in page["messages"]],
                "hasMore": page["hasMore"], "nextCursor": page["nextCursor"]}

    @staticmethod
    def _forecast_fingerprint(account, workdir, user, version, draft, window):
        basis = {"account": account, "workdir": str(workdir), "user": user, "version": version,
                 "draft": draft, "messages": [{"id": item["id"], "side": item["side"],
                    "kind": item.get("kind"), "text": item.get("text"), "time": item.get("time"),
                    "sort": item.get("_sort")} for item in window]}
        encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _verify_forecast_basis(self, account, workdir, user, version, draft, fingerprint):
        current_account, current_workdir = self.source.identity()
        if str(current_account) != account or str(current_workdir) != str(workdir):
            raise ForecastRequestError(409, "account-changed", "当前账号已变化，请刷新会话")
        current_window = self.source.messages(user, FORECAST_SOURCE_WINDOW)
        if self._forecast_fingerprint(account, workdir, user, version, draft, current_window) != fingerprint:
            raise ForecastRequestError(409, "stale-message", "会话消息已变化，请刷新后重试")

    def predict_reply(self, user, account, request_id, draft="", expected_last_message_id=None, member=None):
        ready = getattr(self.source, "require_messages_ready", None)
        if callable(ready):
            ready()
        if source_is_group(self.source,user):
            raise ForecastRequestError(422, "group-unsupported", "群聊无法确定由谁回复，请选择单聊")
        if member is not None:
            raise ForecastRequestError(422, "member-not-applicable", "单聊预测不需要选择成员")
        actual_account, workdir = self.source.identity()
        actual_account = str(actual_account)
        if account != actual_account:
            raise ForecastRequestError(409, "account-changed", "当前账号已变化，请刷新会话")
        version = self.analyzer.analysis_version()
        window = self.source.messages(user, FORECAST_SOURCE_WINDOW)
        self._assert_scope((actual_account, str(Path(workdir).resolve())))
        if not window:
            raise ForecastRequestError(422, "no-context", "当前会话没有可用于预测的消息")
        last_message_id = window[-1]["id"]
        if expected_last_message_id is not None and expected_last_message_id != last_message_id:
            raise ForecastRequestError(409, "stale-message", "会话已有新消息，请刷新后重试")
        text_items = [item for item in window if item.get("kind") == "text" and
                      isinstance(item.get("text"), str) and item["text"].strip()]
        context_items = text_items[-8:]
        while len(context_items) > 1 and any(len(item["text"]) > 4000 for item in context_items):
            context_items.pop(0)
        if context_items and len(context_items[-1]["text"]) > 4000:
            raise ForecastRequestError(422, "content-too-long", "这段内容过长，请缩短草稿或稍后重试")
        context = [{"id": item["id"], "side": item["side"], "text": item["text"],
                    "time": item.get("time", 0)} for item in context_items]
        if not context:
            raise ForecastRequestError(422, "no-text-context", "最近对话没有可用于预测的文字消息")
        fingerprint = self._forecast_fingerprint(actual_account, workdir, user, version, draft, window)
        with self.forecast_lock:
            cached = self.forecast_cache.get(fingerprint)
            if cached is not None:
                self.forecast_cache.move_to_end(fingerprint)
            flight = self.forecast_flights.get(fingerprint) if cached is None else None
            leader = cached is None and flight is None
            if leader:
                flight = {"event": threading.Event(), "error": None, "candidates": None}
                self.forecast_flights[fingerprint] = flight
        if cached is not None:
            candidates = cached
            self._verify_forecast_basis(actual_account, workdir, user, version, draft, fingerprint)
        elif not leader:
            flight["event"].wait()
            if flight["error"] is not None:
                raise flight["error"]
            candidates = flight["candidates"]
            self._verify_forecast_basis(actual_account, workdir, user, version, draft, fingerprint)
        else:
            try:
                session = actual_account + ":" + str(workdir) + ":" + user
                try:
                    forecast = self.analyzer.predict_reply(session, context, draft)
                except RuntimeError as exc:
                    if "ForecastInputTooLongError" in str(exc):
                        raise ForecastRequestError(422, "content-too-long", "这段内容过长，请缩短草稿或稍后重试") from exc
                    raise
                if forecast.get("analysisVersion") != version:
                    raise RuntimeError("reply forecast version mismatch")
                candidates = forecast["candidates"]
                # A message may be edited, withdrawn, or received while the model call waits.
                self._verify_forecast_basis(actual_account, workdir, user, version, draft, fingerprint)
                with self.forecast_lock:
                    self.forecast_cache[fingerprint] = candidates
                    self.forecast_cache.move_to_end(fingerprint)
                    while len(self.forecast_cache) > FORECAST_CACHE_LIMIT:
                        self.forecast_cache.popitem(last=False)
                flight["candidates"] = candidates
            except Exception as exc:
                flight["error"] = exc
                raise
            finally:
                with self.forecast_lock:
                    self.forecast_flights.pop(fingerprint, None)
                    flight["event"].set()
        return {"user": user, "account": actual_account, "requestId": request_id,
                "basisFingerprint": fingerprint, "lastMessageId": last_message_id,
                "candidates": candidates}

    def _schedule_recent(self, key, store, job, scope, limit, standalone=False,window=None):
        state = self.recent_windows.get(key)
        targets=OrderedDict((item['id'],item) for item in (window[-limit:] if window is not None else [])
            if labels_eligible(self.source,item) and item['kind']=='text' and item['text'].strip())
        if window is not None:
            base=store.owner.base
            base.add_resume_targets(key[0],key[2],key[3],list(targets),limit)
            saved,_=base.resume_targets(key[0],key[2],key[3])
            from qq_group_analysis import current_window
            for stable_id in saved:
                current=current_window(self.source,key[0],key[2],stable_id)
                if current and labels_eligible(self.source,current[-1]) and current[-1]['kind']=='text' and current[-1]['text'].strip():
                    targets.setdefault(stable_id,current[-1])
                else:
                    base.complete_resume_targets(key[0],key[2],key[3],[stable_id])
        if state is not None:
            state["limit"] = max(state["limit"], limit)
            if window is not None:
                pending=state.setdefault('pending_targets',OrderedDict())
                if len(set(pending)|set(targets))>2048:
                    raise ValueError('group-analysis-backlog-full')
                pending.update(targets)
                job['backlog']=len(pending)
            if state["iterator"] is not None:
                state["rerun"] = True
            elif window is not None:
                state['window']=window
            return
        counts = {"total": 0, "processed": 0}
        self.recent_windows[key] = {"iterator": None, "counts": counts, "limit": limit,
                                    "rerun": False, "job": job, "background_pending": standalone,'window':window,
                                    'pending_targets':targets}
        job["recent"] = {"id": uuid.uuid4().hex, "status": "queued", "total": 0, "processed": 0}
        self._enqueue((key, "recent-window", limit, store, job, scope), interactive=True)

    def start(self, user, mode, limit, expected_account=None):
        account, workdir, store = self._scoped_identity()
        if expected_account is not None and account != expected_account:
            raise AccountChangedError()
        control_state=store.analysis_state(account,user)
        if self.source_kind=='qq' and control_state!='running':
            return {'id':None,'status':control_state,'total':0,'processed':0}
        if store.cache_suspended(account, LOCAL_SOURCE_ID):
            return {"id": None, "status": "suspended", "total": 0, "processed": 0}
        version = self.analyzer.analysis_version()
        group_window=None
        if self.source_kind=='qq' and source_is_group(self.source,user):
            if mode!='recent':
                raise ValueError('group-portrait-target-required')
            if type(limit) is not int or not 1<=limit<=80:
                raise ValueError('group-label-range-invalid')
            from qq_group_analysis import stream_version
            group_window=list(self.source.messages(user,limit+3))
            version=stream_version(version)
        source_scope = (account, workdir)
        repository = self._qq_analysis_store(account, workdir, store)
        rebuild = False
        if repository is not None:
            store = (repository.bind_labels(user,version,self.analyzer.analysis_version()) if group_window is not None
                     else repository.bind(user, version))
            source_scope = store.scope
            rebuild = store.requires_rebuild
            if rebuild and mode != "recent":
                mode, limit = "incremental", None
        if expected_account is not None:
            self._assert_scope((account, workdir))
        key = (account, str(store.path), user, version)
        with self.jobs_lock:
            current = self.jobs.get(key)
            if current and (current["status"] in ("queued", "running") or key in self.recent_windows):
                if mode == "recent":
                    self._schedule_recent(key, store, current, source_scope, limit,window=group_window)
                    return dict(current)
                if (mode == "incremental" and key in self.recent_windows and
                        current["status"] == "done"):
                    current["requested"] = {"mode": "incremental", "limit": None}
                    self.recent_windows[key]["background_pending"] = True
                    return dict(current)
                if mode == "incremental" and current["requested"]["mode"] != "incremental":
                    current["requested"] = {"mode": "incremental", "limit": None}
                    if key in self.recent_windows and current["status"] == "done":
                        self.recent_windows[key]["background_pending"] = True
                    elif (not self.batch_engine and key not in self.recent_windows and
                          not self._stored_progress(account, user, version, store)["complete"]):
                        self.priority_recent[key] = max(self.priority_recent.get(key, 0), 80)
                elif mode == "incremental" and current["requested"]["mode"] == "incremental":
                    self.incremental_recheck.add(key)
                elif mode == "history" and current["requested"]["mode"] != "incremental" and scope_rank(limit) > scope_rank(current["requested"]["limit"]):
                    current["requested"] = {"mode": "history", "limit": limit}
                    if key in self.recent_windows and current["status"] == "done":
                        self.recent_windows[key]["background_pending"] = True
                    if limit == "all" and not self.batch_engine:
                        self.priority_recent[key] = max(self.priority_recent.get(key, 0), 80)
                return dict(current)
            job = {"id": uuid.uuid4().hex, "status": "queued", "total": 0, "processed": 0,
                   "requested": {"mode": "incremental" if rebuild else mode,
                                 "limit": None if rebuild else limit},
                   **({"dataRevision": store.revision, "rebuilding": rebuild} if repository else {})}
            self.jobs[key] = job
            if mode == "recent":
                self._schedule_recent(key, store, job, source_scope, limit, standalone=True,window=group_window)
                return dict(job)
            if (not self.batch_engine and
                    ((mode == "incremental" and not self._stored_progress(account, user, version, store)["complete"]) or
                     (mode == "history" and limit == "all"))):
                self.priority_recent[key] = 80
            self._enqueue((key, mode, limit, store, job, source_scope))
            return dict(job)

    def _analyze_item(self, account, user, version, store, context, item, scope=None, defer=False,
                      fine=False, portrait_context=None):
        started = time.perf_counter()
        # Attach the local unified-input metadata at the one point where the trusted
        # account/conversation scope is known. Legacy fields stay untouched.
        wire_context = message_input.prepare_messages(context, account_id=account,
                                                      conversation_id=user, source_kind=self.source_kind)
        try:
            session = account + ":" + str(store.path) + ":" + user
            response = (self.analyzer.analyze(session, wire_context, item["id"],
                                              portraitContext=portrait_context, messageLabelsOnly=True)
                        if fine else self.analyzer.analyze(session, wire_context, item["id"]))
        except RuntimeError as exc:
            if str(exc) != "ObservedTextTooLongError":
                raise
            if defer:
                return {"skip": "ObservedTextTooLongError"}
            if scope is not None:
                self._assert_scope(scope)
            (store.skip_fine if fine else store.skip)(account, user, version, item,
                                                       "ObservedTextTooLongError")
            return
        inferred = time.perf_counter()
        validation_version=self.analyzer.analysis_version() if fine and self.source_kind=='qq' and source_is_group(self.source,user) else version
        result = (validate_fine_result if fine else validate_portrait_result)(
            response, validation_version, item["side"],
            **({'relationship_applicable':False} if fine and self.source_kind=='qq' and source_is_group(self.source,user) else {}))
        if fine and item.get('_qqInputBasis'):
            result['qqInputBasis']=item['_qqInputBasis']
        if defer:
            # The incremental caller must scope-verify the whole pending batch before saving.
            return {"result": result, "started": started, "inferred": inferred,
                    "modelMs": float(response.get("_modelMs") or (inferred-started)*1000)}
        if scope is not None:
            self._assert_scope(scope)
        verified = time.perf_counter()
        (store.save_fine if fine else store.save)(account, user, version, item, result)
        finished = time.perf_counter()
        with self.jobs_lock:
            metrics = self.performance.setdefault((account, user, version), {})
            defaults = {
                "count": 0, "modelMs": 0.0, "requestMs": 0.0, "verifyMs": 0.0,
                "saveMs": 0.0, "betweenMs": 0.0, "lastFinished": started,
            }
            for name, value in defaults.items():
                metrics.setdefault(name, value)
            metrics["count"] += 1
            metrics["modelMs"] += float(response.get("_modelMs") or (inferred-started)*1000)
            metrics["requestMs"] += (inferred-started)*1000
            metrics["verifyMs"] += (verified-inferred)*1000
            metrics["saveMs"] += (finished-verified)*1000
            metrics["betweenMs"] += max(0.0, (started-metrics["lastFinished"])*1000)
            metrics["lastFinished"] = finished

    def _history_pages(self, user, highwater, scope=None):
        after = None
        while True:
            if scope is not None:
                self._assert_scope(scope)
            page, cursor = self.source.history_page(user, highwater, after)
            if scope is not None:
                self._assert_scope(scope)
            if cursor is None:
                return
            if after is not None and cursor <= after:
                raise RuntimeError("history cursor did not advance")
            yield page
            after = cursor

    def _take_recent_priority(self, key):
        with self.jobs_lock:
            return self.priority_recent.pop(key, 0)

    def _fine_portrait_context(self, account, user, version, store, item):
        if not self.batch_engine:
            return None
        group = source_is_group(self.source,user)
        if self.source_kind=='qq' and group:
            return None
        member = item.get("senderId") if group else SELF_SUBJECT if self.source_kind=='qq' and item.get('side')=='self' else None
        if group and not member:
            return None
        version=portrait_version(version,self.source,user,member)
        if self.source_kind=='qq' and member is not None:
            store=getattr(getattr(store,'owner',None),'base',store)
            repository=self._qq_analysis_store(account,self.source.identity()[1],store)
            if repository and repository.status(user,version)['stale']:
                return None
        saved = self.batch_engine.snapshot(account, user, version, store, member)
        if not saved:
            return None
        state = saved["state"]
        count = int(state.get("targetCount") or 0)
        if count < 3:
            return None
        parts = [f"画像{count}条"]
        traits = sorted(traits_from_state(state), key=lambda item: item["val"], reverse=True)
        parts.extend(f"{item['label'][:4]}{item['val']}" for item in traits[:2])
        broad = state.get("broad") or {}
        if broad:
            parts.append(f"常见意图{str(max(broad, key=broad.get))[:6]}")
        mood = mood_from_progress(state)
        if mood and mood.get("label"):
            parts.append(f"情绪{str(mood['label'])[:4]}")
        return "；".join(parts)[:60]

    def _analyze_fine_item(self, account, user, version, store, context, item, scope=None,
                           portrait_context=None):
        """Explicit local fine entry point; delegates to the shared analyze/save protocol.

        The fine path never takes the defer branch, so this only names the intent. The
        parameters, model call, validation, skip, persistence and metrics stay in
        ``_analyze_item``; this is not a task-lifecycle redesign.
        """
        return self._analyze_item(account, user, version, store, context, item, scope,
                                  fine=True, portrait_context=portrait_context)

    def _run_fine_recent(self, account, user, version, store, job, scope, limit,window=None,targets=None):
        self._assert_scope(scope)
        window = self.source.messages(user, limit + 3) if window is None else window
        self._assert_scope(scope)
        selected = [(index, item) for index, item in enumerate(window)
                    if index >= len(window) - limit and labels_eligible(self.source,item) and
                    item["kind"] == "text" and item["text"].strip()]
        if targets is not None:
            selected=[(0,item) for item in targets]
        grouped=self.source_kind=='qq' and source_is_group(self.source,user)
        if grouped:
            from qq_group_analysis import history_results
            known=set(history_results(store,account,user,self.analyzer.analysis_version(),store.revision,
                [item['id'] for _,item in selected],source=self.source))
        else:
            known = store.fine_known(account, user, version, [item["id"] for _index, item in selected])
        if grouped and known:
            store.complete_resume_targets(account,user,version,known)
        job["total"] = len(selected)
        job["processed"] = sum(item["id"] in known for _index, item in selected)
        contexts = {}
        if grouped:
            job['processed']=sum(item['id'] in known for _,item in selected)
        since_yield = 0
        for index, item in (selected if grouped else reversed(selected)):
            if item["id"] in known:
                continue
            subject = message_subject(self.source,user,item)
            if subject not in contexts:
                contexts[subject] = self._fine_portrait_context(account, user, version, store, item)
            if grouped:
                from qq_group_analysis import neighborhood
                context=[*neighborhood(window,index),item]
            else:
                context=window[max(0,index-3):index+1]
            if grouped:
                from qq_analysis_store import StaleGroupInputError
                from qq_group_analysis import current_window,input_basis,neighborhood
                # Refresh a changed target, rather than killing unrelated work.
                # A revoked generation/account still raises the regular scope error.
                while True:
                    self._assert_scope(scope)
                    live=current_window(self.source,account,user,item['id'])
                    if not live or not labels_eligible(self.source,live[-1]) or live[-1]['kind']!='text' or not live[-1]['text'].strip():
                        job['invalidated']=job.get('invalidated',0)+1
                        break
                    item=live[-1]
                    item['_qqInputBasis']=input_basis(account,user,self.analyzer.analysis_version(),live,len(live)-1)
                    context=[*neighborhood(live,len(live)-1),item]
                    try:
                        self._analyze_fine_item(account,user,version,store,context,item,scope,portrait_context=None)
                    except StaleGroupInputError:
                        job['invalidated']=job.get('invalidated',0)+1
                        job['recomputed']=job.get('recomputed',0)+1
                        yield
                        continue
                    break
            else:
                self._analyze_fine_item(account, user, version, store,
                                        context, item, scope,
                                        portrait_context=contexts[subject])
            known.add(item["id"])
            job["processed"] += 1
            since_yield += 1
            if grouped or since_yield >= 8 or self._interactive_waiting():
                since_yield = 0
                yield
        if grouped:
            # Backfill during a later inference can invalidate an already committed
            # earlier target. Re-admit those targets before declaring this pass done.
            from qq_group_analysis import current_window,history_results
            recheck=[]
            for _,previous in selected:
                current=current_window(self.source,account,user,previous['id'])
                if (current and labels_eligible(self.source,current[-1]) and current[-1]['kind']=='text' and
                        current[-1]['text'].strip() and previous['id'] not in history_results(store,account,user,
                            self.analyzer.analysis_version(),store.revision,[previous['id']],source=self.source)):
                    recheck.append(current[-1])
            job['_recheck_targets']=recheck

    def _run_visible_priority(self, account, user, version, store, job, scope,
                               highwater, cached_ids, counted_ids, processed_ids, limit):
        if not limit:
            return
        if self.batch_engine:
            yield from self._run_fine_recent(account, user, version, store, job, scope, limit)
            return
        self._assert_scope(scope)
        window = self.source.messages(user, limit + 3)
        self._assert_scope(scope)
        start = max(0, len(window) - limit)
        since_yield = 0
        order = sorted(range(start, len(window)),
                       key=lambda index: (window[index]["side"] != "other", -index))
        for index in order:
            item = window[index]
            analyzed = False
            if ((highwater is None or tuple(item["_sort"]) <= highwater) and
                    item["kind"] == "text" and item["text"].strip()):
                stable_id = item["id"]
                if stable_id not in counted_ids:
                    counted_ids.add(stable_id)
                    job["total"] += 1
                if stable_id not in cached_ids and not store.has(account, user, version, stable_id):
                    self._analyze_item(account, user, version, store,
                                       window[max(0, index - 3):index + 1], item, scope)
                    analyzed = True
                cached_ids.add(stable_id)
                if stable_id not in processed_ids:
                    processed_ids.add(stable_id)
                    job["processed"] += 1
            if analyzed:
                since_yield += 1
                if since_yield >= 8 or self._interactive_waiting():
                    since_yield = 0
                    yield

    def _infer_quoted_batch(self, account, user, version, store, scope, subject, batches, items):
        """Commit an ordered quote-only target batch, including partial-window resumes."""
        context = self.source.preceding_text_context(user, tuple(items[0]["_sort"]))
        remaining = items
        while remaining:
            known = self.batch_engine.known(account, user, version, store, subject, remaining)
            first = next((index for index, item in enumerate(remaining) if item["id"] not in known), None)
            if first is None:
                batches.advance_quoted_backfill(account, user, version, subject,
                                                tuple(remaining[-1]["_sort"]))
                return
            if first:
                batches.advance_quoted_backfill(account, user, version, subject,
                                                tuple(remaining[first-1]["_sort"]))
                remaining = remaining[first:]
            self._assert_scope(scope)
            cursor, offset, context = self.batch_engine.infer(
                account, user, version, store, scope, subject, remaining, context, advance=False)
            completed = [item for item in remaining if tuple(item["_sort"]) < cursor or
                         tuple(item["_sort"]) == cursor and not offset]
            if completed:
                batches.advance_quoted_backfill(account, user, version, subject,
                                                tuple(completed[-1]["_sort"]))
            remaining = [item for item in remaining if tuple(item["_sort"]) > cursor or
                         offset and tuple(item["_sort"]) == cursor]
            yield

    def _run_quoted_backfill(self, account, user, version, store, scope, member=None):
        """Add only previously skipped quoted text inside the saved portrait cursor."""
        if not self.batch_engine:
            return
        subject = self.batch_engine.subject(user, member)
        saved = self.batch_engine.ensure(account, user, version, store, scope, member)
        if saved["cursor"] is None:
            return
        batches = self.batch_engine.store(store)
        checkpoint = batches.begin_quoted_backfill(account, user, version, subject, saved["cursor"])
        if checkpoint["complete"]:
            return
        after = checkpoint["cursor"]
        while True:
            self._assert_scope(scope)
            page, next_after = self.source.quoted_history_page(
                user, checkpoint["ceiling"], after, member=member)
            self._assert_scope(scope)
            if next_after is None:
                batches.advance_quoted_backfill(account, user, version, subject, complete=True)
                return
            if after is not None and next_after <= after:
                raise RuntimeError("quoted backfill cursor did not advance")
            eligible = [item for item in page if item["kind"] == "text" and item["text"].strip()]
            known = self.batch_engine.known(account, user, version, store, subject, eligible)
            pending, characters = [], 0
            for item in page:
                position = tuple(item["_sort"])
                if item["id"] in known or item["kind"] != "text" or not item["text"].strip():
                    continue
                if member is not None and not self.batch_engine.target(item,subject,is_group=source_is_group(self.source,user)):
                    continue
                if self.batch_engine.target(item, subject, is_group=source_is_group(self.source,user)):
                    if pending and (len(pending) >= 12 or characters + len(item["text"]) > 3000):
                        yield from self._infer_quoted_batch(account, user, version, store, scope,
                                                            subject, batches, pending)
                        pending, characters = [], 0
                    pending.append(item)
                    characters += len(item["text"])
                elif source_is_group(self.source,user) and member is None:
                    if pending:
                        yield from self._infer_quoted_batch(account, user, version, store, scope,
                                                            subject, batches, pending)
                        pending, characters = [], 0
                    # Whole-room statistics include the user's own text, without
                    # assigning it a relationship or personality inference.
                    length = len(item["text"])
                    record = {"id": item["id"], "position": list(position),
                              "senderId": item["senderId"], "side": item["side"],
                              "target": False, "startOffset": 0, "endOffset": length,
                              "textLength": length, "complete": True}
                    batch_id = hashlib.sha256(json.dumps(
                        [account, user, version, subject, "quoted-self", item["id"]],
                        ensure_ascii=False).encode()).hexdigest()
                    current = batches.load(account, user, version, subject)
                    self._assert_scope(scope)
                    batches.commit(account, user, version, subject, batch_id=batch_id,
                                   consumed=[record], cursor=current["cursor"],
                                   char_offset=current["charOffset"], context=current["context"],
                                   result=None, is_group=True, advance=False)
                    batches.advance_quoted_backfill(account, user, version, subject, position)
            if pending:
                yield from self._infer_quoted_batch(account, user, version, store, scope,
                                                    subject, batches, pending)
            batches.advance_quoted_backfill(account, user, version, subject, next_after)
            after = next_after
            if self._interactive_waiting():
                yield

    def _run_all_history(self, account, user, version, store, job, scope, key):
        if self.batch_engine:
            yield from self._run_quoted_backfill(account, user, version, store, scope)
            yield from self.batch_engine.incremental(account, user, version, store, job, scope, key)
            return
        self._assert_scope(scope)
        highwater = self.source.history_highwater(user)
        self._assert_scope(scope)
        cached_ids = store.ids(account, user, version)
        counted_ids = set()
        processed_ids = set()
        seen = set()
        first = last = None
        job["total"] = job["processed"] = 0
        job["scope"] = {"mode": "history", "limit": "all", "first": None, "last": None}
        yield from self._run_visible_priority(account, user, version, store, job, scope, highwater,
                                              cached_ids, counted_ids, processed_ids,
                                              self._take_recent_priority(key) or 80)
        context = deque(maxlen=3)
        since_yield = 0
        for page in self._history_pages(user, highwater, scope):
            for item in page:
                priority = self._take_recent_priority(key)
                if priority:
                    yield from self._run_visible_priority(account, user, version, store, job, scope, None,
                                                          cached_ids, counted_ids, processed_ids, priority)
                stable_id = item["id"]
                if stable_id in seen:
                    raise RuntimeError("history snapshot contains duplicate message")
                seen.add(stable_id)
                first = first or stable_id
                last = stable_id
                analyzed = False
                if item["kind"] == "text" and item["text"].strip():
                    if stable_id not in counted_ids:
                        counted_ids.add(stable_id)
                        job["total"] += 1
                    if stable_id not in cached_ids and not store.has(account, user, version, stable_id):
                        self._analyze_item(account, user, version, store, [*context, item], item, scope)
                        analyzed = True
                    cached_ids.add(stable_id)
                    if stable_id not in processed_ids:
                        processed_ids.add(stable_id)
                        job["processed"] += 1
                context.append(item)
                if analyzed:
                    since_yield += 1
                    if since_yield >= 8 or self._interactive_waiting():
                        job["scope"] = {"mode": "history", "limit": "all", "first": first, "last": last}
                        since_yield = 0
                        yield
            job["scope"] = {"mode": "history", "limit": "all", "first": first, "last": last}
            if self._interactive_waiting():
                yield
        priority = self._take_recent_priority(key)
        if priority:
            yield from self._run_visible_priority(account, user, version, store, job, scope, None,
                                                  cached_ids, counted_ids, processed_ids, priority)
        job["scope"] = {"mode": "history", "limit": "all", "first": first, "last": last}

    def _run_incremental(self, account, user, version, store, job, scope, key):
        if self.batch_engine:
            yield from self._run_quoted_backfill(account, user, version, store, scope)
            yield from self.batch_engine.incremental(account, user, version, store, job, scope, key)
            return
        self._assert_scope(scope)
        progress = store.progress(account, user, version)
        cursor = progress["cursor"]
        context = deque(progress["context"], maxlen=3)
        job["phase"] = "incremental" if progress["complete"] else "baseline"
        job["checkpointComplete"] = False
        job["total"] = job["processed"] = progress["eligible"]
        job["scope"] = {"mode": "incremental", "limit": None,
                        "first": None, "last": None}
        highwater = self.source.history_highwater(user)
        self._assert_scope(scope)
        priority = self._take_recent_priority(key)
        if priority:
            priority_job = {"total": 0, "processed": 0}
            yield from self._run_visible_priority(account, user, version, store, priority_job,
                                                  scope, None, set(), set(), set(), priority)
        since_yield = 0
        pending = []
        pending_inferences = 0

        def flush():
            nonlocal cursor, pending_inferences
            if not pending:
                return
            verify_started = time.perf_counter()
            self._assert_scope(scope)
            verified = time.perf_counter()
            saved = []
            for item, position, saved_context, prepared in pending:
                if prepared is not None:
                    if "skip" in prepared:
                        store.skip(account, user, version, item, prepared["skip"])
                    else:
                        save_started = time.perf_counter()
                        store.save(account, user, version, item, prepared["result"])
                        saved.append((prepared, (time.perf_counter() - save_started) * 1000))
                # Results and skips always reach durable storage before their scan cursor.
                store.advance(account, user, version, position, saved_context, item)
                cursor = position
                if item["kind"] == "text" and item["text"].strip():
                    job["total"] += 1
                    job["processed"] += 1
                job["scope"]["last"] = item["id"]
                if job["scope"]["first"] is None:
                    job["scope"]["first"] = item["id"]
            finished = time.perf_counter()
            with self.jobs_lock:
                metrics = self.performance.setdefault((account, user, version), {
                    "count": 0, "modelMs": 0.0, "requestMs": 0.0, "verifyMs": 0.0,
                    "saveMs": 0.0, "betweenMs": 0.0, "lastFinished": verify_started,
                })
                metrics["verifyMs"] += (verified - verify_started) * 1000
                if saved:
                    metrics["betweenMs"] += max(0.0, (saved[0][0]["started"] - metrics["lastFinished"]) * 1000)
                    for index, (prepared, save_ms) in enumerate(saved):
                        metrics["count"] += 1
                        metrics["modelMs"] += prepared["modelMs"]
                        metrics["requestMs"] += (prepared["inferred"] - prepared["started"]) * 1000
                        metrics["saveMs"] += save_ms
                        if index:
                            metrics["betweenMs"] += max(0.0, (prepared["started"] - saved[index - 1][0]["inferred"]) * 1000)
                    metrics["lastFinished"] = finished
            pending.clear()
            pending_inferences = 0

        while highwater is not None and (cursor is None or cursor < highwater):
            self._assert_scope(scope)
            page, next_cursor = self.source.history_page(user, highwater, cursor)
            self._assert_scope(scope)
            if next_cursor is None:
                break
            if cursor is not None and next_cursor <= cursor:
                raise RuntimeError("history cursor did not advance")
            scan_cursor = cursor
            for item in page:
                priority = self._take_recent_priority(key)
                if priority:
                    flush()
                    priority_job = {"total": 0, "processed": 0}
                    yield from self._run_visible_priority(account, user, version, store,
                                                          priority_job, scope, None, set(), set(),
                                                          set(), priority)
                position = tuple(item["_sort"])
                if scan_cursor is not None and position <= scan_cursor:
                    continue
                analyzed = False
                prepared = None
                if item["kind"] == "text" and item["text"].strip():
                    if not store.has(account, user, version, item["id"]):
                        prepared = self._analyze_item(account, user, version, store,
                                                      [*context, item], item, defer=True)
                        analyzed = True
                context.append(item)
                pending.append((item, position, list(context), prepared))
                scan_cursor = position
                if analyzed:
                    pending_inferences += 1
                    since_yield += 1
                interactive = self._interactive_waiting()
                if pending_inferences >= 4 or interactive or since_yield >= 8:
                    flush()
                if since_yield >= 8 or interactive:
                    since_yield = 0
                    yield
            flush()
            if cursor is None or next_cursor > cursor:
                self._assert_scope(scope)
                store.advance(account, user, version, next_cursor, context)
                cursor = next_cursor
            if self._interactive_waiting():
                yield
        self._assert_scope(scope)
        final_cursor = max(cursor, highwater) if cursor is not None and highwater is not None else cursor or highwater
        store.advance(account, user, version, final_cursor, context, complete=True)
        job["checkpointComplete"] = True

    def _run_finite_history(self, account, user, version, store, job, scope, limit):
        if self.batch_engine:
            yield from self.batch_engine.recent(account, user, version, store, job, scope, limit)
            return
        window = self.source.messages(user, limit + 3)
        self._assert_scope(scope)
        selected = window[-limit:]
        cached = store.ids(account, user, version)
        eligible = [item for item in selected if item["kind"] == "text" and item["text"].strip()]
        pending = [item for item in eligible if item["id"] not in cached]
        positions = {message["id"]: index for index, message in enumerate(window)}
        job["total"] = len(eligible)
        job["processed"] = len(eligible) - len(pending)
        job["scope"] = {"mode": "history", "limit": limit,
                        "first": selected[0]["id"] if selected else None,
                        "last": selected[-1]["id"] if selected else None}
        since_yield = 0
        for item in pending:
            analyzed = not store.has(account, user, version, item["id"])
            if analyzed:
                index = positions[item["id"]]
                context = window[max(0, index - 3):index + 4]
                self._analyze_item(account, user, version, store, context, item, scope)
            job["processed"] += 1
            if analyzed:
                since_yield += 1
                if since_yield >= 8 or self._interactive_waiting():
                    since_yield = 0
                    yield
        self._assert_scope(scope)

    def _run_recent_turn(self, key, store, job, scope):
        account, _, user, version = key
        try:
            self._assert_scope(scope)
            with self.jobs_lock:
                state = self.recent_windows.get(key)
                if state is None or state["job"] is not job:
                    return
                if state["iterator"] is None:
                    state["counts"] = {"total": 0, "processed": 0}
                    if state.get('window') is not None:
                        pending=state['pending_targets']
                        targets=list(pending.values())
                        pending.clear()
                        job['backlog']=0
                        state['iterator']=self._run_fine_recent(account,user,version,store,state['counts'],scope,state['limit'],window=state['window'],targets=targets)
                    else:
                        state["iterator"] = self._run_visible_priority(
                        account, user, version, store, state["counts"], scope,
                        None, set(), set(), set(), state["limit"])
                job["recent"] = {**job["recent"], "status": "running"}
                if job["requested"]["mode"] == "recent":
                    job["status"] = "running"
                    job["total"] = state["counts"]["total"]
                    job["processed"] = state["counts"]["processed"]
                iterator = state["iterator"]
            try:
                next(iterator)
            except StopIteration:
                self._publish_local_analysis(store,complete=self.source_kind=='qq' and source_is_group(self.source,user))
                with self.jobs_lock:
                    state = self.recent_windows.get(key)
                    if state is None or state["job"] is not job:
                        return
                    counts = state["counts"]
                    recheck=counts.pop('_recheck_targets',[])
                    if recheck:
                        state['pending_targets'].update((item['id'],item) for item in recheck)
                        counts['invalidated']=counts.get('invalidated',0)+len(recheck)
                        counts['recomputed']=counts.get('recomputed',0)+len(recheck)
                        job['backlog']=len(state['pending_targets'])
                    if job["requested"]["mode"] == "recent":
                        job["total"] = counts["total"]
                        job["processed"] = counts["processed"]
                    for name in ('invalidated','recomputed'):
                        job[name]=job.get('_prior_'+name,0)+counts.get(name,0)
                    if state["rerun"] or state.get('pending_targets'):
                        for name in ('invalidated','recomputed'):
                            job['_prior_'+name]=job.get(name,0)
                        state["iterator"] = None
                        state["rerun"] = False
                        job["recent"] = {**job["recent"], "status": "queued",
                                         "total": counts["total"], "processed": counts["processed"]}
                        self._enqueue((key, "recent-window", state["limit"], store, job, scope), interactive=True)
                    else:
                        job["recent"] = {**job["recent"], "status": "done",
                                         "total": counts["total"], "processed": counts["processed"]}
                        del self.recent_windows[key]
                        requested = job["requested"]
                        if state["background_pending"] and requested["mode"] != "recent":
                            job["status"] = "queued"
                            self._enqueue((key, requested["mode"], requested["limit"], store, job, scope))
                        elif requested["mode"] == "recent":
                            job["status"] = "done"
            else:
                with self.jobs_lock:
                    state = self.recent_windows.get(key)
                    if state is None or state["job"] is not job:
                        return
                    counts = state["counts"]
                    for name in ('invalidated','recomputed'):
                        job[name]=job.get('_prior_'+name,0)+counts.get(name,0)
                    if job["requested"]["mode"] == "recent":
                        job["total"] = counts["total"]
                        job["processed"] = counts["processed"]
                    job["recent"] = {**job["recent"], "status": "running",
                                     "total": counts["total"], "processed": counts["processed"]}
                    self._enqueue((key, "recent-window", state["limit"], store, job, scope), interactive=True)
        except AnalysisInterrupted as exc:
            with self.jobs_lock:
                state=self.recent_windows.get(key)
                if state is not None and state['job'] is job:
                    del self.recent_windows[key]
                job['status']=exc.state
                job['recent']={**job.get('recent',{}),'status':exc.state}
        except Exception as exc:
            with self.jobs_lock:
                state = self.recent_windows.get(key)
                background_pending = state is not None and state["job"] is job and state["background_pending"]
                if state is not None and state["job"] is job:
                    del self.recent_windows[key]
                job["recent"] = {**job.get("recent", {}), "status": "error", "error": str(exc)[:200]}
                if job["requested"]["mode"] == "recent" or background_pending:
                    job["status"] = "error"
                    job["error"] = str(exc)[:200]

    def _worker(self):
        while True:
            _, _, task = self.tasks.get()
            if task is None:
                self.tasks.task_done()
                return
            key, mode, limit, store, job, source_scope = task
            with self.jobs_lock:
                current=(self.batch_engine.member_jobs.get((*key,limit)) is job if mode=='batch-subject' and self.batch_engine
                         else self.jobs.get(key) is job)
            if not current or job.get('status') in ('paused','cancelled'):
                self.tasks.task_done()
                continue
            if store.cache_suspended(key[0], LOCAL_SOURCE_ID):
                job["status"] = "suspended"
                self.tasks.task_done()
                continue
            if self.active_model_source_mode != "local":
                # Local Laya analysis must not compete with an active API source. The
                # persisted cursor is kept; a later local switch resumes from it.
                job["status"] = "paused"
                self.tasks.task_done()
                continue
            if mode == "batch-subject" and self.batch_engine:
                try:
                    member_key = (*key, limit)
                    iterator = self.quoted_backfill_iterators.get(member_key)
                    if iterator is None:
                        account, _path, user, version = key
                        iterator = self._run_quoted_backfill(account, user, version, store,
                                                              source_scope, limit)
                        self.quoted_backfill_iterators[member_key] = iterator
                    try:
                        next(iterator)
                    except StopIteration:
                        self.quoted_backfill_iterators.pop(member_key, None)
                        self.batch_engine.member_turn(key, limit, store, job, source_scope)
                        if job.get('status')=='done':
                            self._publish_local_analysis(store,complete=True)
                    else:
                        job["status"] = "queued"
                        self._enqueue(task)
                except Exception as exc:
                    self.quoted_backfill_iterators.pop((*key, limit), None)
                    job["status"]=exc.state if isinstance(exc,AnalysisInterrupted) else "error"
                    if not isinstance(exc,AnalysisInterrupted):job["error"]=str(exc)[:200]
                finally:
                    self.tasks.task_done()
                continue
            if mode == "recent-window":
                try:
                    self._run_recent_turn(key, store, job, source_scope)
                finally:
                    self.tasks.task_done()
                continue
            account, _, user, version = key
            terminal = True
            try:
                self._assert_scope(source_scope)
                with self.jobs_lock:
                    requested = job["requested"]
                    if requested["mode"] == "incremental":
                        mode, limit = "incremental", None
                    elif requested["mode"] == "history" and scope_rank(requested["limit"]) > scope_rank(limit):
                        mode, limit = "history", requested["limit"]
                    job["status"] = "running"
                if mode in ("incremental", "history"):
                    iterator = self.history_iterators.get(key)
                    if iterator is not None and self.history_iterator_modes.get(key) != (mode, limit):
                        iterator.close()
                        self.history_iterators.pop(key, None)
                        self.history_iterator_modes.pop(key, None)
                        iterator = None
                    if iterator is None:
                        iterator = (self._run_incremental(account, user, version, store, job,
                                                          source_scope, key) if mode == "incremental" else
                                    self._run_all_history(account, user, version, store, job,
                                                          source_scope, key) if limit == "all" else
                                    self._run_finite_history(account, user, version, store, job,
                                                             source_scope, limit))
                        self.history_iterators[key] = iterator
                        self.history_iterator_modes[key] = (mode, limit)
                    try:
                        next(iterator)
                    except StopIteration:
                        self._publish_local_analysis(store, complete=(mode == "incremental" or limit == "all"))
                        with self.jobs_lock:
                            requested = job["requested"]
                            if mode == "incremental" and key in self.incremental_recheck:
                                self.incremental_recheck.discard(key)
                                self.history_iterators.pop(key, None)
                                self.history_iterator_modes.pop(key, None)
                                job["status"] = "queued"
                                terminal = False
                                self._enqueue((key, "incremental", None, store, job, source_scope))
                            elif requested["mode"] == "incremental" and mode != "incremental":
                                self.history_iterators.pop(key, None)
                                self.history_iterator_modes.pop(key, None)
                                job["status"] = "queued"
                                terminal = False
                                self._enqueue((key, "incremental", None, store, job, source_scope))
                            elif (mode == "history" and requested["mode"] == "history" and
                                  scope_rank(requested["limit"]) > scope_rank(limit)):
                                self.history_iterators.pop(key, None)
                                self.history_iterator_modes.pop(key, None)
                                job["status"] = "queued"
                                terminal = False
                                self._enqueue((key, "history", requested["limit"], store, job, source_scope))
                            else:
                                job["status"] = "done"
                    else:
                        terminal = False
                        self._enqueue((key, mode, limit, store, job, source_scope))
                    continue
                raise RuntimeError("unsupported analysis mode")
            except AnalysisInterrupted as exc:
                with self.jobs_lock:
                    job['status']=exc.state
            except Exception as exc:
                with self.jobs_lock:
                    job["status"] = "error"
                    job["error"] = str(exc)[:200]
            finally:
                if terminal and self.jobs.get(key) is job:
                    self.history_iterators.pop(key, None)
                    self.history_iterator_modes.pop(key, None)
                    with self.jobs_lock:
                        self.priority_recent.pop(key, None)
                        self.incremental_recheck.discard(key)
                self.tasks.task_done()

    @published_analysis_read
    def analysis(self, user,limit=80):
        account, workdir, store = self._scoped_identity()
        version = self.analyzer.analysis_version()
        if self.source_kind=='qq' and source_is_group(self.source,user):
            from qq_group_analysis import stream_version
            if type(limit) is not int or not 1<=limit<=80:
                raise ValueError('group-label-range-invalid')
            version=stream_version(version)
        repository = self._qq_analysis_store(account, workdir, store)
        revision = repository.status(user, version) if repository else {}
        job_key = repository.job_key(user, version) if repository else (account, str(store.path), user, version)
        if self.source_kind=='qq' and source_is_group(self.source,user):
            job_key=(account,str(store.path),user,version)
        if store.cache_suspended(account, LOCAL_SOURCE_ID):
            return {"results": {}, "job": {"id": None, "status": "suspended", "total": 0,
                                             "processed": 0}, "affinity": None,
                    "affinityCount": 0, "modelState": self.analyzer.model["state"],
                    "modelProvider": self.analyzer.model.get("provider"),
                    "analysisVersion": version, "mood": None, "performance": {},
                    "analysisUnit": "message", "account": account}
        self._focus(job_key)
        progress = self._stored_progress(account, user, version, store)
        saved = self.batch_engine.snapshot(account, user, version, store) if self.batch_engine else None
        summary = saved["state"] if saved else store.summary(account, user, version)
        results = store.recent(account, user, version)
        results.update(store.fine_view(account, user, version))
        group = source_is_group(self.source,user)
        if self.source_kind=='qq' and group:
            from qq_group_analysis import history_results
            window=list(self.source.messages(user,limit))
            results=history_results(store,account,user,self.analyzer.analysis_version(),revision['dataRevision'],
                [item['id'] for item in window],source=self.source)
            eligible=[item for item in window if labels_eligible(self.source,item) and item['kind']=='text' and item['text'].strip()]
            pending=sum(item['id'] not in results for item in eligible)
            revision={**revision,'hasPublishedAnalysis':bool(results),'stale':False,
                'analysisRevision':revision['dataRevision'] if results else None,'pendingCount':pending,
                'publishedCount':len(results),'publicationUnit':'verified-message-input'}
        with self.jobs_lock:
            job = dict(self.jobs.get(job_key,
                                     {"id": "", "status": "idle", "total": progress["eligible"],
                                      "processed": progress["eligible"],
                                      "phase": "incremental" if progress["complete"] else "baseline",
                                      "checkpointComplete": progress["complete"]}))
            performance = {name: round(value, 2) for name, value in
                           self.performance.get((account, user, version), {}).items()
                            if name != "lastFinished"}
        if self.batch_engine and job.get("requested", {}).get("mode") == "recent":
            job["phase"] = "incremental" if progress["complete"] else "baseline"
            job["checkpointComplete"] = progress["complete"]
        if self.source_kind=='qq' and group:
            job.update(published=revision['publishedCount'],pending=revision['pendingCount'],
                dataRevision=revision['dataRevision'],publicationUnit='message')
            job={name:value for name,value in job.items() if not name.startswith('_')}
        self._assert_scope((account, workdir))
        return {"results": {id: {key: val for key, val in result.items() if key != "score"}
                            for id, result in results.items()},
                "job": job, "affinity": None if group else affinity_from_progress(summary),
                "affinityCount": summary["scoreCount"] if not group else 0,
                "modelState": self.analyzer.model["state"],
                "modelProvider": self.analyzer.model.get("provider"), "analysisVersion": version,
                "mood": mood_from_progress(summary), "performance": performance,
                "analysisUnit": "message",
                "account": account, "analysisControl":store.analysis_state(account,user), **revision}

    def _legacy_profile_state(self, account, user, version, store, workdir, subject):
        if self.source_kind=='qq' and not source_is_group(self.source,user):
            if subject==SELF_SUBJECT:
                return empty_profile_state()
            if subject==user:
                subject=None
        def read_texts(refs):
            self._assert_scope((account, workdir))
            if hasattr(self.source, "texts_for_refs"):
                texts = dict(self.source.texts_for_refs(user, refs, with_ids=True))
            else:
                texts = {}
                selected = {stable_id for _shard, _local, stable_id in refs}
                for page in self._history_pages(user, self.source.history_highwater(user), (account, workdir)):
                    texts.update((item["id"], item["text"]) for item in page
                                 if item["id"] in selected and item["kind"] == "text")
            self._assert_scope((account, workdir))
            return texts
        return store.profile_state(account, user, version, subject, read_texts)

    @published_analysis_read
    def profile(self, user, member=None, retry=False):
        account, workdir, store = self._scoped_identity()
        if self.source_kind=='qq' and source_is_group(self.source,user) and member is None:
            return self._group_overview(account,user)
        version = portrait_version(self.analyzer.analysis_version(),self.source,user,member)
        published_store = store
        repository = self._qq_analysis_store(account, workdir, store)
        revision = repository.status(user, version) if repository else {}
        analysis_scope = (account, workdir)
        control_state=store.analysis_state(account,user)
        if repository and control_state=="running" and not store.cache_suspended(account, LOCAL_SOURCE_ID):
            store = repository.bind(user, version)
            analysis_scope = store.scope
        group = source_is_group(self.source,user)
        selected = member or user
        subject = target_subject(self.source,user,member) or None
        if hasattr(self.source, "profile_metadata"):
            metadata = self.source.profile_metadata(user, member)
            contact, members = metadata["contact"], metadata["members"]
            count, text_count = metadata["count"], metadata["textCount"]
        else:
            group_count, group_text_count, members = self.source.stats(user)
            if member and (not group or member not in {item["id"] for item in members}):
                raise ValueError("unknown member")
            contact = self.source.contact(selected)
            count, text_count = (self.source.stats(user, subject)[:2] if subject else
                                 (group_count, group_text_count))
        def read_legacy_texts(refs):
            self._assert_scope((account, workdir))
            if hasattr(self.source, "texts_for_refs"):
                texts = dict(self.source.texts_for_refs(user, refs, with_ids=True))
            else:
                texts = {}
                analyzed_ids = {stable_id for _shard, _local_id, stable_id in refs}
                for page in self._history_pages(user, self.source.history_highwater(user), (account, workdir)):
                    texts.update((message["id"], message["text"]) for message in page
                                 if message["id"] in analyzed_ids and message["kind"] == "text")
            self._assert_scope((account, workdir))
            return texts
        if self.source_kind=="qq" and control_state!="running":
            saved=self.batch_engine.snapshot(account,user,version,published_store) if self.batch_engine else None
            state=saved["state"] if saved else empty_profile_state()
            profile_job={"status":control_state,"checkpointComplete":bool(saved and saved["complete"])}
        elif store.cache_suspended(account, LOCAL_SOURCE_ID):
            state = empty_profile_state()
            profile_job = {"status": "suspended", "checkpointComplete": False}
        elif self.batch_engine and (self.active_model_source_mode != "local" or
                                    (callable(getattr(self.analyzer, "local_model_status", None)) and
                                     self.local_model_status()["state"] != "ready")):
            saved = self.batch_engine.ensure(account, user, version, store, analysis_scope, member)
            state = saved["state"]
            profile_job = {"status": "missing-model" if self.active_model_source_mode == "local"
                           else "inactive-source", "checkpointComplete": saved["complete"]}
        elif self.batch_engine and self.source_kind=='qq' and group and text_count==0:
            state=empty_profile_state()
            profile_job={'status':'insufficient-evidence','checkpointComplete':True,'analysisUnit':'batch'}
        elif self.batch_engine:
            saved = self.batch_engine.ensure(account, user, version, store, analysis_scope, member)
            state = saved["state"]
            backfill_pending = self.batch_engine.store(store).quoted_backfill_pending(
                account, user, version, self.batch_engine.subject(user, member), saved["cursor"])
            if member:
                profile_job = self.batch_engine.request_member(account, user, version, store,
                                                                (account, workdir), member,
                                                                max(text_count, state["count"] + 1)
                                                                if backfill_pending else text_count, retry)
            else:
                self.batch_engine.focused_member = None
                key = (account, str(store.path), user, version)
                self._focus(key)
                highwater = self.source.history_highwater(user)
                needs_work = (revision.get("stale", False) or backfill_pending or not saved["complete"] or bool(saved["charOffset"]) or
                              highwater is not None and
                              (saved["cursor"] is None or highwater > tuple(saved["cursor"])))
                with self.jobs_lock:
                    current = dict(self.jobs.get(key, {}))
                    fine_active = key in self.recent_windows
                current_mode = current.get("requested", {}).get("mode")
                portrait_active = (current_mode in ("incremental", "history") and
                                   (current.get("status") in ("queued", "running") or
                                    current.get("status") == "error" and not retry) and
                                   (not fine_active or current.get("analysisUnit") == "batch"))
                if portrait_active:
                    profile_job = current
                elif needs_work:
                    if not (fine_active and current_mode == "incremental"):
                        current = self.start(user, "incremental", None)
                    profile_job = ({"status": "queued", "phase": "baseline" if not saved["complete"] else "incremental",
                                    "checkpointComplete": False} if fine_active else current)
                else:
                    profile_job = {"status": "done", "checkpointComplete": True}
                profile_job = {**{name: value for name, value in profile_job.items() if name != "recent"},
                               "analysisUnit": "batch"}
        else:
            state = store.profile_state(account, user, version, subject, read_legacy_texts)
            profile_job = None
        if repository and self.batch_engine and not published_store.cache_suspended(account, LOCAL_SOURCE_ID):
            previous = self.batch_engine.snapshot(account, user, version, published_store, member)
            if previous is not None:
                state = previous["state"]
        distributions = {field: [{"label": label, "probability": value / state["count"]}
                                 for label, value in state[field].items()] if state["count"] else []
                         for field in ("emotion", "intent")}
        mood = mood_from_progress(state)
        keywords = keywords_from_counts(state["words"])
        inference = None if group and not member else mbti_from_totals(
            state["targetCount"], state["supported"], state["axes"], version)
        traits = traits_from_state(state)
        summary = summary_from_aggregate(state["targetCount"], state["broad"], mood, keywords, group and not member)
        if self.source_kind=='qq' and subject==SELF_SUBJECT and isinstance(summary,str):
            summary=summary.replace('该联系人消息','我在本单聊的消息')
        elif self.source_kind=='qq' and group and isinstance(summary,str):
            summary=summary.replace('该联系人消息','该成员在本群的消息')
        self._assert_scope((account, workdir))
        return {"username": selected, **contact, "isGroup": group, "members": members if group else [],
                **({'targetSide':'self' if subject==SELF_SUBJECT or group and contact.get('isSelf') else 'other','targetId':subject,'basisStrategy':'qq-target-v1',
                    'analysisVersion':version} if self.source_kind=='qq' else {}),
                "stats": {"messageCount": count, "textCount": text_count, "analyzedCount": state["count"],
                          "participantCount": (1 if count else 0) if subject else len(members)},
                "affinity": None if group or subject==SELF_SUBJECT else affinity_from_progress(state),
                **distributions, "keywords": keywords,
                "mood": mood, "mbti": inference["type"] if inference else None,
                "mbtiInference": inference, "traits": traits, "traitsBasis": "chat-behaviour",
                "summary": summary, "suggestions": [], "dataStatus": "analyzed" if state["count"] and state["count"] >= text_count else "partial" if state["count"] else "unanalyzed",
                **({"job": profile_job, "analysisUnit": "batch"} if self.batch_engine else {}),
                "account": account, "analysisControl":store.analysis_state(account,user), **revision}

    def _reserve_api_request(self, job, scope, store):
        with self.api_lock:
            self._assert_scope(scope)
            if job.get('status') in ('paused','cancelled'):raise AnalysisInterrupted(job['status'])
            if self.source_kind=='qq':
                base=store.owner.base if hasattr(store,'owner') else store
                with self.source.read_library(scope[0]):
                    usage=base.reserve_api_request(scope[0],store.user,self.active_model_source_id)
                job['usage']=usage
            job['requestsIssued']=job.get('requestsIssued',0)+1

    def grant_analysis_budget(self, requested_account, user, requests):
        with self.api_lock:
            account,workdir,base=self._scoped_identity()
            if self.source_kind!='qq' or account!=requested_account:raise AccountChangedError()
            if self.active_model_source_mode!='api':raise ValueError('api-source-not-active')
            with self.source.read_library(account) as (_,library):
                if library.revision(account,user) is None:raise ValueError('unknown QQ conversation')
                usage=base.grant_api_requests(account,user,self.active_model_source_id,requests)
            return {'account':account,'user':user,'apiUsage':usage}

    def analysis_control_status(self, requested_account, user):
        account,workdir,base=self._scoped_identity()
        if self.source_kind!='qq' or account!=requested_account:raise AccountChangedError()
        with self.source.read_library(account) as (_,library):
            if library.revision(account,user) is None:raise ValueError('unknown QQ conversation')
            return {'account':account,'user':user,'analysisControl':base.analysis_state(account,user),'modelMode':self.active_model_source_mode,
                'apiUsage':base.api_request_budget(account,user,self.active_model_source_id) if self.active_model_source_mode=='api' else None}

    def control_analysis(self, requested_account, user, action):
        if self.source_kind!='qq' or action not in ('pause','cancel','resume'):
            raise ValueError('invalid-analysis-control')
        # Match API publication's lock order; the source lock serializes local commits.
        with self.api_lock:
            account,workdir,base=self._scoped_identity()
            if account!=requested_account:raise AccountChangedError()
            with self.source.read_library(account) as (_,library),base.profile_lock:
                if library.revision(account,user) is None:raise ValueError('unknown QQ conversation')
                state={'pause':'paused','cancel':'cancelled','resume':'running'}[action]
                if action=="resume" and base.analysis_state(account,user)=="running":
                    return {"account":account,"user":user,"analysisControl":state}
                base.set_analysis_state(account,user,state)
                repository=self.qq_analysis_stores.get((account,str(base.path)))
                if repository:
                    repository.stages={key:stage for key,stage in repository.stages.items() if stage.user!=user}
                with self.jobs_lock:
                    if self.batch_engine:
                        for member_key,member_job in self.batch_engine.member_jobs.items():
                            if member_key[0]==account and member_key[2]==user:
                                if member_job.get('status') in ('queued','running'):
                                    member_job['status']='cancelled' if action=='resume' else state
                                self.batch_engine.member_iterators.pop(member_key,None)
                    for key,job in self.jobs.items():
                        if key[0]==account and key[2]==user and job.get('status') in ('queued','running'):
                            job['status']='cancelled' if action=='resume' else state
                            self.recent_windows.pop(key,None)
                            self.history_iterators.pop(key,None)
                            self.history_iterator_modes.pop(key,None)
                            self.priority_recent.pop(key,None)
                    for key in list(self.quoted_backfill_iterators):
                        if key[0]==account and key[2]==user:self.quoted_backfill_iterators.pop(key,None)
                for registry in (self.api_jobs,self.api_portrait_jobs):
                    for key,job in registry.items():
                        if key[0]==account and key[1]==user and job.get('status') in ('queued','running'):
                            job['status']='cancelled' if action=='resume' else state
                self.api_condition.notify_all()
        return {'account':account,'user':user,'analysisControl':state}

    def _group_overview(self,account,user):
        metadata=self.source.profile_metadata(user)
        members=metadata['members']
        return {'account':account,'username':user,**metadata['contact'],'isGroup':True,'members':members,
                'objectiveOverview':True,'stats':{'messageCount':metadata['count'],'textCount':metadata['textCount'],
                    'participantCount':sum(bool(row.get('count')) for row in members),'analyzedCount':0},
                'affinity':None,'emotion':[],'intent':[],'keywords':[],'traits':[],'mood':None,'mbti':None,'mbtiInference':None,
                'summary':f"本机已读范围内保存了{metadata['count']}条消息；选择一个成员查看此人在本群的画像。",
                'dataStatus':'overview','job':{'status':'done','checkpointComplete':True},'analysisUnit':'objective'}

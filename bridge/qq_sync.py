"""Account-bound, cancellable forward synchronization with durable window replay.

Production live access remains gated until G1; injected fake services exercise
the actual coordinator, backward coverage and recent reconciliation.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import replace
from contextlib import contextmanager

from backend_contracts import AccountUnavailableError
from account_store import AccountNotFound, account_id
from qq_connector import QQConnector, ConnectorError, SyncCancelled, SyncPolicy, failure_code
from qq_identity import account_key, canonical_uin
from qq_message_store import QQMessageStore, CHECKPOINT_FIELDS
from qq_sync_config import SyncConnectionStore, validate_connection_input
from qq_normalize import UID
import qq_sync_work as work_plan


TERMINAL_FAILURES = {"auth-required", "account-changed", "identity-unavailable", "unsupported-version",'unverified-version',
                     "protocol-invalid", "source-rejected", "configuration-invalid"}


class QQSync:
    METADATA_WAIT_SECONDS = 2

    def __init__(self, accounts, *, live_validated=False, config_store=None, policy=None,
                 connector_factory=QQConnector, on_commit=None, autostart=True,
                 monotonic=time.monotonic, wall_ms=lambda: int(time.time() * 1000), initial_read=False):
        if type(live_validated) is not bool or type(initial_read) is not bool:
            raise ValueError("invalid-live-validation-state")
        self.accounts, self.backend = accounts, accounts.backend
        self.source = self.backend.source
        self.live_validated = live_validated
        self.config = config_store or SyncConnectionStore(accounts.runtime_dir / "qq-connection.json")
        self.policy = policy or SyncPolicy()
        self.connector_factory, self.on_commit = connector_factory, on_commit
        self.monotonic, self.wall_ms = monotonic, wall_ms
        self.condition = threading.Condition(threading.RLock())
        self.wake = threading.Event()
        self.closed = False
        self.epoch = 0
        self.busy = False
        self.metadata_waiting = 0
        self.suspended = False
        self.connector = None
        self.due = {}
        self.work_due = {}
        self.work_status = {}
        self.request_times = deque()
        self.requests_total = 0
        self.state = "unavailable" if not live_validated else "disabled"
        self.reason = "connector-awaiting-validation" if not live_validated else None
        self.conversations = {}
        self.last_success = None
        self.worker = None
        self.background_work_enabled = True
        self.initial_read = initial_read
        self.group_ready = True
        self.compatibility=None
        if autostart:
            self.worker = threading.Thread(target=self._loop, name="qq-sync", daemon=True)
            self.worker.start()

    def public(self):
        try:
            config = self.config.public()
        except ConnectorError:
            config = {"enabled": False, "baseUrl": None, "ownerUin": None, "tokenConfigured": False}
        persisted_success = None
        if config["ownerUin"]:
            try:
                from qq_ingest_audit import last_success
                with self.source.read_library(account_key(config["ownerUin"])) as (_, library):
                    persisted_success = last_success(library.connection, account_key(config["ownerUin"]))
            except Exception:
                # A temporarily unavailable or changing account cannot borrow
                # another binding's in-memory success timestamp.
                pass
        with self.condition:
            conversations = json.loads(json.dumps(self.conversations))
            account = account_key(config["ownerUin"]) if config["ownerUin"] else None
            for (owner, user, kind), payload in self.work_status.items():
                if owner == account:
                    conversations.setdefault(user, {})[kind] = {**payload,
                        "retryAvailable": payload["attempts"] >= self.policy.max_attempts}
            return {**config, "liveValidated": self.live_validated, "state": self.state, "reason": self.reason,
                    'compatibility':self.compatibility,
                    "account": account_key(config["ownerUin"]) if config["ownerUin"] else None,
                    "lastSuccessAtMs": persisted_success, "requestsTotal": self.requests_total,
                    "conversations": conversations}

    def _set(self, state, reason=None):
        with self.condition:
            self.state, self.reason = state, reason

    def _drain(self):
        with self.condition:
            self.suspended = True
            self.epoch += 1
            self.wake.set()
            self.condition.notify_all()
            if not self.condition.wait_for(lambda: not self.busy, timeout=20):
                raise ConnectorError("sync-stopping")

    def configure(self, base, owner, token=None):
        base, owner = validate_connection_input(base, owner, token)
        self._drain()
        try:
            previous = self.config.public()
        except ConnectorError:
            previous = {}
        try:
            configured = self.config.configure(base, owner, token)
        except Exception as error:
            self._set("paused", error.code if isinstance(error, ConnectorError) else "configuration-unavailable")
            raise
        # Keep the previous timestamp only for the same endpoint and owner.
        if (previous.get("ownerUin"), previous.get("baseUrl")) != (
                configured["ownerUin"], configured["baseUrl"]):
            self.last_success = None
        self.connector = None
        self.due.clear()
        self.conversations.clear()
        self._set("disabled" if self.live_validated else "unavailable",
                  None if self.live_validated else "connector-awaiting-validation")
        self.resume_binding()
        return self.public()

    def connect(self,*,allow_unverified=False):
        if type(allow_unverified) is not bool:raise ValueError('invalid-version-consent')
        if not self.live_validated:
            raise ConnectorError("connector-awaiting-validation")
        self._drain()
        saved = self.config.read()
        self.config.set_enabled(False)
        self.connector = None
        try:
            candidate = self.connector_factory(saved["baseUrl"], self.config.token(), policy=self.policy)
            identity = candidate.identity(candidate.client(on_request=self._charge_request))
        except Exception as error:
            self._set("offline", failure_code(error))
            raise ConnectorError(failure_code(error)) from None
        if identity["ownerUin"] != saved["ownerUin"]:
            self._set("paused", "account-changed")
            raise ConnectorError("account-changed")
        self.compatibility=identity.get('compatibility')
        if self.compatibility and self.compatibility['requiresConsent']:
            if not allow_unverified and saved.get('acceptedUnverifiedVersion')!=identity['version']:
                self._set('paused','unverified-version')
                raise ConnectorError('unverified-version')
            candidate.accepted_unverified_version=identity['version']
            if allow_unverified:self.config.accept_version(identity['version'])
        account = account_key(saved["ownerUin"])
        with self.accounts.lock, self.source.lock:
            if self.accounts.deleting or self.backend.closing or self.accounts.store.is_deleting(account):
                raise ConnectorError("account-clearing")
            try:
                current = self.source.identity()[0]
            except AccountUnavailableError:
                current = None
            if current is not None and current != account:
                raise ConnectorError("account-changed")
            self.source.attach(saved["ownerUin"])
            from qq_entities import qq_avatar
            with self.source.read_library(account) as (_,library):
                library.set_person(account,uin=identity['ownerUin'],uid=identity.get('ownerUid'),
                                   nickname=identity.get('name') or '',avatar_url=qq_avatar(identity['ownerUin']))
            _, directory = self.source.identity()
            try:
                nickname = self.accounts.store.resolve(account_id(account)).get("nickname", "")
            except AccountNotFound:
                nickname = ""
            self.accounts.store.register(account, directory, owner_uin=saved["ownerUin"], nickname=nickname)
        self.connector = candidate
        self.config.set_enabled(True)
        self._set("online")
        self.resume_binding()
        return self.public()

    def connect_standard(self):
        if not self.live_validated:
            raise ConnectorError("connector-awaiting-validation")
        from qq_standard_connection import standard_connection
        base, owner, token = standard_connection()
        # Do not replace a working account binding with another logged-in user.
        with self.accounts.lock, self.source.lock:
            try:
                current = self.source.identity()[0]
            except AccountUnavailableError:
                current = None
            if current is not None and current != account_key(owner):
                raise ConnectorError("account-changed")
        self.configure(base, owner, token)
        return self.connect()

    def disconnect(self, *, clear_token=False):
        self._drain()
        self.config.set_enabled(False)
        if clear_token:
            self.config.clear_token()
        self.connector = None
        self._set("disabled" if self.live_validated else "unavailable",
                  None if self.live_validated else "connector-awaiting-validation")
        self.resume_binding()
        return self.public()

    def pause_binding(self):
        self._drain()

    def resume_binding(self):
        with self.condition:
            # Durable work belongs to the library; no parked deadline or display
            # snapshot may survive reconnecting or rebinding that library.
            self.due.clear()
            self.work_due.clear()
            self.work_status.clear()
            self.conversations.clear()
            self.suspended = False
        self.wake.set()

    def selection_changed(self, account, user, selected):
        with self.condition:
            if not selected:
                # Reject late scans and hide status for the removed scope.
                self.epoch += 1
                self.conversations.pop(user, None)
            self.due.pop((account, user), None)
            for kind in ("history", "reconcile"):
                self.work_due.pop((account, user, kind), None)
                self.work_status.pop((account, user, kind), None)
        self.wake.set()

    def reading_changed(self,account,user,enabled):
        self.selection_changed(account,user,enabled)
        if not enabled:
            with self.condition:self.conversations[user]={'state':'paused','reason':'conversation-read-paused'}
        self.wake.set()

    def stop_account(self, account):
        saved = self.config.read()
        if saved["ownerUin"] and account_key(saved["ownerUin"]) == account:
            self.disconnect()

    def _charge_request(self):
        now = self.monotonic()
        with self.condition:
            while self.request_times and self.request_times[0] <= now - 60:
                self.request_times.popleft()
            if len(self.request_times) >= self.policy.requests_per_minute:
                raise ConnectorError("request-budget-exhausted")
            self.request_times.append(now)
            self.requests_total += 1

    @contextmanager
    def _metadata_operation(self):
        binding = self.source.binding_token()
        with self.condition:
            if not self.live_validated:
                raise ConnectorError("connector-awaiting-validation")
            if self.suspended or self.closed:
                raise ConnectorError("sync-busy")
            epoch = self.epoch
            self.metadata_waiting += 1
            try:
                available = self.condition.wait_for(
                    lambda: not self.busy or self.suspended or self.closed or self.epoch != epoch,
                    timeout=self.METADATA_WAIT_SECONDS)
                if self.epoch != epoch or self.suspended or self.closed:
                    raise ConnectorError("scope-changed")
                if not available or self.busy:
                    raise ConnectorError("sync-busy")
                self.busy = True
            finally:
                self.metadata_waiting -= 1
        try:
            saved = self.config.read()
            if not saved["enabled"]:
                raise ConnectorError("sync-disabled")
            account = account_key(saved["ownerUin"])
            if binding[0] != account:
                raise ConnectorError("account-changed")
            self._check(epoch, binding)
            connector = self.connector or self.connector_factory(saved["baseUrl"], self.config.token(), policy=self.policy)
            yield saved, connector, epoch, binding
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()

    def contacts(self, page=1):
        with self._metadata_operation() as (saved, connector, epoch, binding):
            try:
                return connector.contacts(saved["ownerUin"], page=page,
                    check=lambda: self._check(epoch, binding), on_request=self._charge_request)
            except SyncCancelled:
                raise ConnectorError("scope-changed") from None
            except Exception as error:
                raise ConnectorError(failure_code(error)) from None

    def add_contact(self, peer_uin, name=""):
        if not isinstance(name, str) or len(name) > 256:
            raise ValueError("invalid-contact-name")
        with self._metadata_operation() as (saved, connector, epoch, binding):
            account, directory = self.source.verified_identity(messages=True)
            try:
                info = connector.resolve_peer(saved["ownerUin"], peer_uin,
                    check=lambda: self._check(epoch, binding), on_request=self._charge_request)
                info["name"] = name or info.get('name') or info["peerUin"]
                @contextmanager
                def commit_scope():
                    with self.source.lock:
                        self._check(epoch, binding)
                        yield
                library = QQMessageStore(directory / "messages.sqlite")
                try:
                    library.ingest(account, info["conversationKey"], [], conversation=info,
                        cancel=lambda: self._check(epoch, binding), commit_scope=commit_scope)
                    from qq_entities import qq_avatar
                    library.set_person(account,uin=info['peerUin'],uid=info['peerUid'],nickname=info['name'],avatar_url=qq_avatar(info['peerUin']))
                    library.set_conversation_profile(account,info['conversationKey'],name=info['name'],avatar_url=qq_avatar(info['peerUin']),source_version='6.3.0')
                    if self.initial_read and library.counts(account, info["conversationKey"])[0] == 0:
                        end = self.wall_ms()
                        start = max(0, end - 90 * 86400000)
                        initial = self.connector_factory(saved["baseUrl"], self.config.token(),
                            policy=replace(self.policy, max_pages=4, max_messages=200))
                        result = initial.scan(saved["ownerUin"], info["peerUid"], start, end,
                            peer_uin=info["peerUin"], check=lambda: self._check(epoch, binding),
                            on_request=self._charge_request)
                        self._check(epoch, binding)
                        library.ingest(account, info["conversationKey"], result["records"],
                            cancel=lambda: self._check(epoch, binding), commit_scope=commit_scope,
                            now_ms=end, receipt={"kind": "history", "format": "qce-api",
                                "status": result["status"], "reason": result["reason"],
                                "windowStartMs": start, "windowEndMs": end,
                                "sourceVersion": result.get("version"),
                                "rejectedRows": result["counts"].get("rowsRejected", 0)})
                finally:
                    library.close()
                self._check(epoch, binding)
                self.backend.set_conversation_selected(account, info["conversationKey"], True)
                if self.initial_read and self.on_commit:
                    try:
                        self.on_commit(account, info["conversationKey"])
                    except Exception:
                        # Saved messages remain usable even if the model is unavailable.
                        pass
                return {"account": account, "user": info["conversationKey"], "selected": True}
            except SyncCancelled:
                raise ConnectorError("scope-changed") from None
            except Exception as error:
                raise ConnectorError(failure_code(error)) from None

    def add_group(self,group_code):
        with self._metadata_operation() as (saved,connector,epoch,binding):
            account,directory=self.source.verified_identity(messages=True)
            info=connector.group_metadata(saved['ownerUin'],group_code,
                check=lambda:self._check(epoch,binding),on_request=self._charge_request)
            self._check(epoch,binding)
            @contextmanager
            def commit_scope():
                with self.source.lock:
                    self._check(epoch,binding)
                    yield
            library=QQMessageStore(directory/'messages.sqlite')
            try:
                library.ensure_conversation(account,info['conversationKey'],kind='group',group_code=info['groupCode'],display_name=info['name'])
                library.set_conversation_profile(account,info['conversationKey'],name=info['name'],avatar_url=info['avatar'],source_version='6.3.0')
                library.set_members(account,info['conversationKey'],info['members'])
                if self.group_ready and self.initial_read and library.target_counts(account,info['conversationKey'])[0]==0:
                    end=self.wall_ms();start=max(0,end-90*86400000)
                    initial=self.connector_factory(saved['baseUrl'],self.config.token(),policy=replace(self.policy,max_pages=4,max_messages=200))
                    result=initial.scan(saved['ownerUin'],info['groupCode'],start,end,kind='group',
                        check=lambda:self._check(epoch,binding),on_request=self._charge_request)
                    library.ingest(account,info['conversationKey'],result['records'],now_ms=end,
                        cancel=lambda:self._check(epoch,binding),commit_scope=commit_scope,
                        receipt={'kind':'history','format':'qce-api','status':result['status'],'reason':result['reason'],
                            'windowStartMs':start,'windowEndMs':end,'sourceVersion':result.get('version'),
                            'rejectedRows':result['counts'].get('rowsRejected',0)})
            finally:
                library.close()
            self._check(epoch,binding)
            self.backend.set_conversation_selected(account,info['conversationKey'],True)
            return {'account':account,'user':info['conversationKey'],'selected':True,'kind':'group','members':len(info['members'])}

    def retry(self, account, user, work_kind="tail"):
        if work_kind not in {"tail", "history", "reconcile"}:
            raise ValueError("invalid-sync-kind")
        if not self.live_validated:
            raise ConnectorError("connector-awaiting-validation")
        self._drain()
        try:
            current, directory = self.source.verified_identity(messages=True)
            owner = self.config.read()["ownerUin"]
            if not owner or account_key(owner) != account or current != account or user not in self.backend.selection_store.get(account)["selectedSessions"]:
                raise ConnectorError("account-changed")
            library = QQMessageStore(directory / "messages.sqlite")
            try:
                if work_kind == "tail":
                    previous = library.checkpoint(account, user)
                    if previous is not None:
                        checkpoint = {name: previous[name] for name in CHECKPOINT_FIELDS}
                        checkpoint["attempts"] = 0
                        library.ingest(account, user, [], checkpoint=checkpoint)
                else:
                    previous = library.sync_work(account, user, work_kind)
                    if previous:
                        payload = previous["payload"]
                        payload.update(attempts=0, state="pending", reason=None)
                        library.ingest(account, user, [], sync_work=(work_kind, previous["revision"], payload))
                        self.work_status[(account, user, work_kind)] = payload
            finally:
                library.close()
            self.due.pop((account, user), None)
            self.work_due.pop((account, user, work_kind), None)
            self._set("online" if self.config.read()["enabled"] else "disabled")
        finally:
            self.resume_binding()
        return self.public()

    def _check(self, epoch, binding):
        with self.condition:
            cancelled = self.closed or epoch != self.epoch or self.backend.closing
        if cancelled or self.source.binding_token() != binding:
            raise SyncCancelled()

    def _checkpoint(self, previous, start, end, now, status, reason=None):
        attempts = (previous or {}).get("attempts", 0)
        complete = status in ("complete", "complete-empty")
        return {"cursor_version": 1, "window_start_ms": start, "window_end_ms": end,
                "scanned_through_ms": end if complete else (previous or {}).get("scanned_through_ms", 0),
                "last_commit_time_ms": now, "last_commit_count": 0,
                "last_task_status": status, "attempts": 0 if complete else attempts + int(reason != "request-budget-exhausted"),
                "overlap_ms": self.policy.overlap_ms,
                "state": "COMMITTED" if complete else "PARTIAL" if status == "partial" else "DISCONNECTED",
                "last_error": reason if status == "error" else None,
                "partial_reason": reason if status == "partial" else None}

    def run_once(self, user, *, now_ms=None, work_kind="tail"):
        if work_kind not in {"tail", "history", "reconcile"}:
            raise ValueError("invalid-sync-kind")
        with self.condition:
            if self.busy:
                raise ConnectorError("sync-busy")
            if self.closed or not self.live_validated:
                raise ConnectorError("connector-awaiting-validation")
            if self.suspended or self.busy or self.metadata_waiting:
                raise ConnectorError("sync-paused")
            self.busy = True
            epoch = self.epoch
        try:
            saved = self.config.read()
            if not saved["enabled"]:
                raise ConnectorError("sync-disabled")
            owner, account = saved["ownerUin"], account_key(saved["ownerUin"])
            binding = self.source.binding_token()
            self._check(epoch, binding)
            current, directory = self.source.verified_identity(messages=True)
            if current != account:
                raise ConnectorError("account-changed")
            selection = self.backend.selection_store
            if user not in selection.get(account)["selectedSessions"]:
                raise ConnectorError("conversation-not-selected")
            if user in selection.paused_reading(account):raise ConnectorError('conversation-read-paused')
            with self.source.read_library(account) as (_, existing):
                metadata = next((dict(row) for row in existing.list_conversations(account) if row["conversation_key"] == user), None)
                previous = existing.checkpoint(account, user)
                if work_kind == "tail":
                    for kind in ("history", "reconcile"):
                        known = existing.sync_work(account, user, kind)
                        if known:
                            self.work_status[(account, user, kind)] = known["payload"]
                        else:
                            self.work_status.pop((account, user, kind), None)
                            self.work_due.pop((account, user, kind), None)
                if work_kind != "tail":
                    existing.ensure_sync_work()
                    saved_work = existing.sync_work(account, user, work_kind)
            peer_uid = (metadata["peer_uid"] or user[2:]) if metadata is not None and user.startswith("u:") else None
            group=metadata is not None and metadata['kind']=='group'
            if group:
                if not self.group_ready:
                    raise ConnectorError('group-awaiting-validation')
                peer_uid=metadata['group_code']
                if not isinstance(peer_uid,str) or user!='g:'+canonical_uin(peer_uid):
                    raise ConnectorError('group-identity-invalid')
            elif not isinstance(peer_uid, str) or not UID.fullmatch(peer_uid) or user != "u:" + peer_uid:
                raise ConnectorError("peer-identity-invalid")
            now = self.wall_ms() if now_ms is None else now_ms
            if type(now) is not int or now < 0:
                raise ValueError("invalid-sync-time")
            attempts = (previous or {}).get("attempts", 0)
            if work_kind != "tail":
                if previous is None:
                    raise ConnectorError("tail-window-required")
                payload = saved_work["payload"] if saved_work else work_plan.initial(work_kind,
                    previous["window_start_ms"] if work_kind == "history" else now,
                    max(0, now - self.policy.recent_ms) if work_kind == "reconcile" else 0)
                if work_kind == "reconcile" and payload["window"] is None:
                    payload = work_plan.initial(work_kind, now, max(0, now - self.policy.recent_ms))
                if payload["window"] is None:
                    self.work_status[(account, user, work_kind)] = payload
                    self.work_due[(account, user, work_kind)] = float("inf")
                    return {"account": account, "user": user, "state": "complete", "kind": work_kind}
                attempts = payload["attempts"]
                if attempts >= self.policy.max_attempts:
                    self.work_status[(account, user, work_kind)] = payload
                    self.work_due[(account, user, work_kind)] = float("inf")
                    return {"account": account, "user": user, "state": "partial", "reason": "retry-limit", "kind": work_kind}
                start, end = payload["window"]
            elif previous and previous["last_task_status"] in ("partial", "error"):
                if attempts >= self.policy.max_attempts:
                    self._set("partial", previous["partial_reason"] or previous["last_error"])
                    self.due[(account, user)] = float("inf")
                    return {"state": "partial", "reason": "retry-limit", "account": account, "user": user}
                start, end = previous["window_start_ms"], previous["window_end_ms"]
            else:
                frontier = (previous or {}).get("scanned_through_ms", 0)
                start = max(0, frontier - self.policy.overlap_ms) if frontier else max(0, now - self.policy.recent_ms)
                end = min(now, start + self.policy.window_ms)
            if end < start:
                raise ConnectorError("clock-before-checkpoint")
            connector = self.connector or self.connector_factory(saved["baseUrl"], self.config.token(), policy=self.policy)
            self.connector = connector
            self._set("fetching")
            display_name = metadata["display_name"] or metadata["peer_uin"] or ("已选群聊" if group else "已选单聊")
            if work_kind == "tail":
                self.conversations[user] = {"state": "fetching", "name": display_name, "windowStartMs": start, "windowEndMs": end}
            try:
                scan_options = {"max_seconds": min(3, self.policy.max_seconds)} if work_kind != "tail" else {}
                if group:
                    scan_options['kind']='group'
                result = connector.scan(owner, peer_uid, start, end, peer_uin=metadata["peer_uin"],
                    attempt=min(attempts, self.policy.max_attempts - 1),
                    check=lambda: self._check(epoch, binding), on_request=self._charge_request, **scan_options)
                self._check(epoch, binding)
                checkpoint = self._checkpoint(previous, start, end, now, result["status"], result["reason"])
                checkpoint["last_commit_count"] = len(result["records"])
                records = result["records"]
                error = None
            except SyncCancelled:
                raise
            except Exception as exception:
                error = failure_code(exception)
                checkpoint = self._checkpoint(previous, start, end, now, "error", error)
                records = []
                result = {"status": "error", "reason": error, "counts": {}}
            if work_kind != "tail":
                checkpoint = None
                next_work = work_plan.advance(work_kind, payload, result["status"], result["reason"], now,
                    max_attempts=self.policy.max_attempts, split_at=min((row["time_ms"] for row in records), default=None))
                work_write = (work_kind, saved_work["revision"] if saved_work else 0, next_work)
            else:
                work_write = None
            @contextmanager
            def commit_scope():
                # Membership is held stable through COMMIT. Read the selection DB
                # before taking the source lock; never wait for model SQL under it.
                with selection.lock:
                    if user not in selection.get(account)["selectedSessions"]:
                        raise SyncCancelled()
                    if user in selection.paused_reading(account):raise SyncCancelled()
                    with self.source.lock:
                        self._check(epoch, binding)
                        yield
            library = QQMessageStore(directory / "messages.sqlite")
            try:
                outcome = library.ingest(account, user, records, checkpoint=checkpoint, now_ms=now,
                    cancel=lambda: self._check(epoch, binding), commit_scope=commit_scope, sync_work=work_write,
                    receipt={"kind": "forward" if work_kind == "tail" else work_kind, "format": "qce-api",
                        "status": result["status"], "reason": result["reason"],
                        "windowStartMs": start, "windowEndMs": end,
                        "sourceVersion": result.get("version"), "rejectedRows": result["counts"].get("rowsRejected", 0)},
                    receipt_clock=self.wall_ms if now_ms is None else None)
            finally:
                library.close()
            if work_kind != "tail":
                self.work_status[(account, user, work_kind)] = next_work
                interval = self.policy.history_seconds if work_kind == "history" else self.policy.reconcile_seconds
                self.work_due[(account, user, work_kind)] = self.monotonic() + interval
                if result["reason"] == "request-budget-exhausted":
                    self.work_due[(account, user, work_kind)] = self.monotonic() + self._quota_delay()
                if next_work["attempts"] >= self.policy.max_attempts or (work_kind == "history" and next_work["window"] is None):
                    self.work_due[(account, user, work_kind)] = float("inf")
                if outcome["inserted"] or outcome["recalled"] or outcome["revised"] or outcome["conflicts"]:
                    if self.on_commit:
                        try:
                            self._check(epoch, binding)
                            self.on_commit(account, user)
                        except Exception:
                            pass
                if error in TERMINAL_FAILURES:
                    self.config.set_enabled(False)
                    self.connector = None
                    self._set("paused", error)
                elif error and error != "request-budget-exhausted":
                    self._set("offline", error)
                else:
                    self._set("online")
                return {"account": account, "user": user, "kind": work_kind,
                        "state": next_work["state"], "reason": next_work["reason"], **outcome}
            self.conversations[user] = {"state": result["status"], "reason": result["reason"], "name": display_name,
                "windowStartMs": start, "windowEndMs": end, "scannedThroughMs": checkpoint["scanned_through_ms"],
                "attempts": checkpoint["attempts"], "retryAvailable": checkpoint["attempts"] >= self.policy.max_attempts,
                "rowsAccepted": len(records), "counts": result["counts"]}
            if result.get('compatibility'):
                self.compatibility=result['compatibility']
            complete = result["status"] in ("complete", "complete-empty")
            if complete:
                self.last_success = now
                self._set("online")
                if outcome["inserted"] or outcome["recalled"] or outcome["revised"] or outcome["conflicts"]:
                    if self.on_commit:
                        try:
                            self._check(epoch, binding)
                            self.on_commit(account, user)
                        except Exception:
                            self.conversations[user]["analysisPending"] = True
            elif error in TERMINAL_FAILURES:
                self.config.set_enabled(False)
                self.connector = None
                self._set("paused", error)
            else:
                self._set("partial" if result["status"] == "partial" else "offline", result["reason"])
            retry = min(60, self.policy.focused_seconds * 2 ** checkpoint["attempts"]) if not complete else self.policy.focused_seconds
            if result["reason"] == "request-budget-exhausted":
                retry = self._quota_delay()
            self.due[(account, user)] = self.monotonic() + retry
            return {"account": account, "user": user, "state": result["status"], "reason": result["reason"], **outcome}
        except SyncCancelled:
            self._set("paused", "scope-changed")
            return {"state": "cancelled", "reason": "scope-changed"}
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()

    def _quota_delay(self):
        with self.condition:
            return max(1, self.request_times[0] + 60 - self.monotonic() + .1) if self.request_times else 1

    def _focused_conversation(self, account):
        # Backend.analysis/profile focus a scoped job key, never a bare user ID.
        # A focus left behind by an account switch cannot accelerate this owner.
        key = getattr(self.backend, "focused_key", None)
        if isinstance(key, tuple) and len(key) == 4 and key[0] == account and isinstance(key[2], str):
            return key[2]
        return None

    def _loop(self):
        while not self.closed:
            self.wake.wait(.5)
            self.wake.clear()
            if not self.live_validated:
                continue
            if self.suspended or self.busy or self.metadata_waiting:
                continue
            try:
                saved = self.config.read()
                if not saved["enabled"] or self.backend.closing:
                    continue
                account = account_key(saved["ownerUin"])
                if self.source.binding_token()[0] is None:
                    # Automatic resume may reopen only an already registered intact
                    # library. Explicit connect is the only bootstrap/create path.
                    item = self.accounts.store.resolve(account_id(account))
                    if not (self.accounts.store._workdir(item["account"]) / "messages.sqlite").is_file():
                        self._set("paused", "local-library-unavailable")
                        continue
                    self.connect()
                if self.source.binding_token()[0] != account:
                    self._set("paused", "account-changed")
                    continue
                selected = self.backend.selection_store.get(account)["selectedSessions"]
                paused=self.backend.selection_store.paused_reading(account)
                selected=[user for user in selected if user not in paused]
                if not self.group_ready:
                    selected=[user for user in selected if self.source.conversation_kind(user)=='friend']
                focused = self._focused_conversation(account)
                if focused not in selected:
                    focused = None
                if focused is not None:
                    with self.condition:
                        key = (account, focused)
                        deadline = self.due.get(key)
                        state = self.conversations.get(focused, {}).get("state")
                        if state in ("complete", "complete-empty") and deadline is not None and math.isfinite(deadline):
                            # Opening a background chat must not retain its old
                            # 20-second deadline. Partial/error retry delays and
                            # parked retry-limit work remain untouched.
                            self.due[key] = min(deadline, self.monotonic() + self.policy.focused_seconds)
                ran = False
                for user in sorted(selected, key=lambda user: (user != focused, self.due.get((account, user), 0))):
                    if self.monotonic() < self.due.get((account, user), 0):
                        continue
                    self.run_once(user)
                    ran = True
                    if user != focused:
                        self.due[(account, user)] = max(self.due.get((account, user), 0), self.monotonic() + self.policy.background_seconds)
                    break
                if not ran and self.background_work_enabled:
                    # Background quanta share the same request budget and worker.
                    # A due forward window always gets first choice.
                    for kind in ("reconcile", "history"):
                        candidates = [user for user in selected if self.monotonic() >= self.work_due.get((account, user, kind), 0)]
                        if not candidates:
                            continue
                        user = min(candidates, key=lambda user: self.work_due.get((account, user, kind), 0))
                        self.run_once(user, work_kind=kind)
                        break
            except ConnectorError as error:
                self._set("offline", error.code)
            except Exception:
                self._set("offline", "connection-unavailable")

    def close(self):
        with self.condition:
            self.closed = True
        self._drain()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=20)
            if self.worker.is_alive():
                raise ConnectorError("sync-stopping")

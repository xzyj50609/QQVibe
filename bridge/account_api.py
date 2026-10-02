"""Attach account management to the local UI without touching WeChat source files."""
from __future__ import annotations

import threading
import time
import json
import os
import uuid
from pathlib import Path

from account_store import AccountConflict, AccountStore, account_id, _check_root, _regular
from snapshot_cache import try_forget_account, wait_forget_account


class AccountAPI:
    def __init__(self, backend, data_dir, snapshot_root=None, stable_keys_dir=None, *,
                 store=None, forget_live=None, forget_inactive=None):
        self.backend = backend
        self.store = store if store is not None else AccountStore(data_dir, snapshot_root, stable_keys_dir)
        self.forget_live = forget_live
        self.forget_inactive = forget_inactive
        self.lock = threading.RLock()
        self.registered = {}
        self.deleting = False
        self.runtime_dir = Path(data_dir).resolve().parent / "real-client-runtime"
        self.no_recovery_marker = self.runtime_dir / "no-auto-recovery.json"

    def _mark_no_recovery(self):
        """Keep the clearing account from being reimported during the active-account exit."""
        _check_root(self.runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.no_recovery_marker.exists():
            _regular(self.no_recovery_marker)
        temporary = self.runtime_dir / ("no-auto-recovery.json.tmp-" + uuid.uuid4().hex)
        try:
            temporary.write_text(json.dumps({"version": 1, "bridgePid": os.getpid(),
                                             "reason": "account-cleared"}) + "\n", encoding="utf-8")
            os.replace(temporary, self.no_recovery_marker)
        finally:
            temporary.unlink(missing_ok=True)

    def _unmark_no_recovery(self):
        _check_root(self.runtime_dir)
        if _regular(self.no_recovery_marker):
            self.no_recovery_marker.unlink()

    def observe(self, sessions):
        """Register once per real login identity; ordinary polling does no DB discovery."""
        account = sessions.get("account")
        own = sessions.get("self") or {}
        if not account:
            return
        display = (str(own.get("username") or ""), str(own.get("name") or ""))
        with self.lock:
            if self.deleting:
                return
            if self.registered.get(account) == display:
                return
            with self.backend.source.lock:
                if sessions.get("messagesReady", True):
                    actual, workdir, _ = self.backend._scoped_identity()
                else:
                    verified = getattr(self.backend.source, "verified_identity", None)
                    if callable(verified):
                        actual, workdir = verified(messages=False)
                    else:
                        actual, workdir = self.backend.source.identity()
                if actual != account:
                    raise AccountConflict("账号已变化，请刷新")
                self.store.register(actual, workdir, *display)
                self.registered[account] = display

    def _live_account(self, deleting=False):
        source = self.backend.source
        if getattr(source, "dynamic_account", False):
            selection = source.active_account_locator()
            if selection is None:
                if deleting:
                    from live_source import discovery
                    if discovery.find_weixin_processes():
                        raise AccountConflict("暂无法确认当前微信账号，请稍后重试")
                return None
            location = getattr(selection, "account_dir", selection)
            return Path(location).name
        return str(source.identity()[0])

    def list(self):
        with self.lock, self.backend.source.lock:
            return self.store.list(self._live_account())

    def delete(self, identifier):
        source = self.backend.source
        with self.lock:
            if self.deleting:
                raise AccountConflict("账号清理正在进行")
            item = self.store.resolve(identifier)
            account = item["account"]
            current = self._live_account(deleting=True) == account
            self.deleting = True
        paused = False
        marked = False
        try:
            if current:
                # Drain requests and writers before removing derived data. A failed removal
                # can resume this bridge; a successful clear closes its model and exits.
                pause = getattr(self.backend, "pause_for_account_clear", None)
                resume = getattr(self.backend, "resume_after_failed_account_clear", None)
                shutdown = getattr(self.backend, "shutdown", None)
                if not all(callable(action) for action in (pause, resume, shutdown)):
                    raise AccountConflict("无法安全停止当前账号读取")
                pause(account)
                paused = True
                self._mark_no_recovery()
                marked = True
                def guard(owned):
                    if owned != account:
                        raise AccountConflict("账号范围已变化")
                result = self.store.delete(identifier, guard=guard, forget=self.forget_live or wait_forget_account)
                paused = False
                shutdown()
            else:
                with source.lock:
                    checked_at, checks, observed = 0.0, 0, None
                    def guard(owned):
                        nonlocal checked_at, checks, observed
                        if checks < 2 or time.monotonic() - checked_at >= 0.5:
                            observed = self._live_account(deleting=True)
                            checked_at = time.monotonic()
                            checks += 1
                        if observed == owned:
                            raise AccountConflict("账号已成为当前账号，请刷新后重试")
                        with self.backend.jobs_lock:
                            busy_job = any(key[0] == owned and job.get("status") in ("running", "queued")
                                           for key, job in self.backend.jobs.items())
                            busy_recent = any(key[0] == owned for key in self.backend.recent_windows)
                            engine = getattr(self.backend, "batch_engine", None)
                            busy_member = bool(engine and any(key[0] == owned and
                                job.get("status") in ("running", "queued")
                                for key, job in engine.member_jobs.items()))
                            if busy_job or busy_recent or busy_member:
                                raise AccountConflict("账号分析尚未结束，请稍后重试")
                    result = self.store.delete(identifier, guard=guard, forget=self.forget_inactive or try_forget_account)
                    forget_keys = getattr(source, "forget_account", None)
                    if callable(forget_keys):
                        forget_keys(account)
            with self.lock:
                for scope in list(self.backend.stores):
                    if account_id(scope[0]) == identifier:
                        del self.backend.stores[scope]
                self.registered.pop(account, None)
            return {**result, "current": current, "exitApp": current}
        except Exception:
            if paused:
                resume()
                if marked:
                    self._unmark_no_recovery()
            raise
        finally:
            with self.lock:
                self.deleting = False

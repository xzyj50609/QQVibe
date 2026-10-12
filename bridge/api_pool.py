"""Parallel API insight workers: one inference process per concurrent call.

NodeAnalysis already multiplexes requests while its condition releases the lock.
This pool provides an explicit bounded budget across its API-only processes; it
does not assume that another process automatically improves provider throughput.

Parallelism is per conversation. Insight jobs are keyed by `(account, user, source_id)`
and a repeated request for the same key returns the running job, so extra capacity only
helps when several conversations are analysed at once; one conversation is never split.
Portrait statistics stay sequential per subject and keep using their own analyzer.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend_contracts import ROOT
from node_analysis import NodeAnalysis
from product_profile import current_product

DEFAULT_API_WORKERS = 1
MAX_API_WORKERS = 4
#: Consecutive provider rate limits before the effective limit steps down by one.
API_RATE_LIMIT_STREAK = 2
#: Consecutive clean provider turns before the effective limit steps back up by one.
API_RATE_LIMIT_RECOVER = 8

_SETTINGS_FILE = current_product().state_dir("real-client-runtime", ROOT) / "api-workers.json"


def api_worker_settings(settings_path=None):
    """Configured API parallelism ceiling.

    Defaults to one worker, which is the previous single-process behaviour. Raise it
    through `WECHATVIBE_API_WORKERS` or
    `.local/real-client-runtime/api-workers.json` (`{"workers": N}`).
    """
    workers = os.environ.get("QQVIBE_API_WORKERS", os.environ.get("WECHATVIBE_API_WORKERS"))
    if workers is None:
        try:
            data = json.loads(Path(settings_path or _SETTINGS_FILE).read_text(encoding="utf-8"))
            workers = data.get("workers")
        except (OSError, ValueError, AttributeError):
            workers = None
    try:
        count = int(workers)
    except (TypeError, ValueError):
        count = DEFAULT_API_WORKERS
    return {"workers": count if 1 <= count <= MAX_API_WORKERS else DEFAULT_API_WORKERS}


def save_api_worker_settings(workers, settings_path=None):
    """Persist the API parallelism choice next to the other runtime state."""
    path = Path(settings_path or _SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"workers": int(workers)}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class ApiAnalyzerPool:
    """Hands out API-only analyzers, at most one lease per analyzer at a time.

    `primary` is resolved through a callable instead of captured, so replacing
    `Backend.api_analyzer` (which the test doubles do) keeps working: slot 0 always
    resolves to the current primary and is never closed here.
    """

    def __init__(self, primary, *, factory=None, settings_path=None):
        self.condition = threading.Condition()
        self.primary = primary if callable(primary) else (lambda: primary)
        self.factory = factory or (lambda: NodeAnalysis(api_only=True))
        self.settings_path = settings_path
        self.maximum = api_worker_settings(settings_path)["workers"]
        self.limit = self.maximum
        # Slot 0 mirrors the primary; extras are created only when work needs them.
        self.entries = [(self.primary(), False)]
        self.active = 0
        self.rate_limit_streak = 0
        self.clean_streak = 0
        self.downshifts = 0
        self.generation = 0
        self.closed = False
        self.waiters = {"shared": [], "portrait": [], "probe": []}
        self.provider_waiters = []
        self.dedicated_active = {"portrait": 0, "probe": 0}

    def configure(self, workers=None):
        """Resize the ceiling and remember it. An explicit choice takes effect at once,
        including lifting an earlier rate-limit downshift. Shrinking parks the extra
        analyzers: they keep serving a lease they already hold, and `close_extras` drops
        the rest."""
        with self.condition:
            if self.closed:
                raise RuntimeError("model-source-changed")
            if workers is not None:
                maximum = max(1, min(MAX_API_WORKERS, int(workers)))
                save_api_worker_settings(maximum, self.settings_path)
                self.maximum = maximum
                self.limit = self.maximum
            if self.limit > self.maximum:
                self.limit = self.maximum
            self.condition.notify_all()
        return self.status()

    def status(self):
        with self.condition:
            return {"workers": self.maximum, "max": MAX_API_WORKERS, "limit": self.limit,
                    "active": self.active, "spawned": 0 if self.closed else max(0, len(self.entries) - 1),
                    "totalActive": self.active + sum(self.dedicated_active.values()),
                    "totalLimit": self.limit,
                    "waiting": sum(map(len, self.waiters.values())),
                    "rateLimitStreak": self.rate_limit_streak, "downshifts": self.downshifts}

    def _spawn_locked(self):
        self.entries.append((self.factory(), False))
        return len(self.entries) - 1

    @contextmanager
    def lease(self, timeout=180.0, *, cancelled=None, validate=None):
        """Reserve one analyzer for the duration of a single provider turn.

        Waiting happens here rather than at the provider, so raising the ceiling turns
        straight into concurrent turns. The timeout is only a safety net: a caller that
        waited longer than the provider's own 180s budget would fail anyway.
        """
        deadline = time.monotonic() + timeout
        ticket = object()
        with self.condition:
            generation = self.generation
            self.waiters["shared"].append(ticket)
            self.provider_waiters.append(ticket)
        index = None
        try:
            while index is None:
                self._validate_waiter(cancelled, validate)
                with self.condition:
                    self._check_epoch(generation)
                    if (self.waiters["shared"][0] is ticket and self.provider_waiters[0] is ticket and
                            self.active + sum(self.dedicated_active.values()) < self.limit):
                        free = [i for i, (_, busy) in enumerate(self.entries) if i < self.limit and not busy]
                        if free:
                            index = free[0]
                        elif len(self.entries) < self.limit:
                            index = self._spawn_locked()
                    if index is not None:
                        analyzer = self.primary() if index == 0 else self.entries[index][0]
                        self.entries[index] = (analyzer, True)
                        self.active += 1
                        self.waiters["shared"].remove(ticket)
                        self.provider_waiters.remove(ticket)
                        self.condition.notify_all()
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("API analyzer pool exhausted")
                    self.condition.wait(min(remaining, .05))
            self._validate_waiter(cancelled, validate)
            with self.condition:
                self._check_epoch(generation)
            yield analyzer
        finally:
            with self.condition:
                if ticket in self.waiters["shared"]:
                    self.waiters["shared"].remove(ticket)
                if ticket in self.provider_waiters:
                    self.provider_waiters.remove(ticket)
                if index is not None:
                    self.entries[index] = (self.entries[index][0], False)
                    self.active -= 1
                    if self.closed and not self.active:
                        self.entries = [(self.primary(), False)]
                self.condition.notify_all()

    @staticmethod
    def _validate_waiter(cancelled, validate):
        # Never invoke application callbacks while holding the pool condition.
        if cancelled is not None and cancelled():
            raise RuntimeError("model-source-changed")
        if validate is not None:
            validate()

    def _check_epoch(self, generation):
        if self.closed or generation != self.generation:
            raise RuntimeError("model-source-changed")

    @contextmanager
    def dedicated(self, lane, timeout=240.0, *, cancelled=None, validate=None):
        """One bounded slot for each existing portrait/probe process.

        Shared label/guidance capacity remains 1..4; two dedicated slots retain
        lane isolation. The combined envelope is always shared limit + 2.
        """
        if lane not in self.dedicated_active:
            raise ValueError("invalid API lane")
        ticket = object()
        deadline = time.monotonic() + timeout
        acquired = False
        with self.condition:
            generation = self.generation
            self.waiters[lane].append(ticket)
            self.provider_waiters.append(ticket)
        try:
            while not acquired:
                self._validate_waiter(cancelled, validate)
                with self.condition:
                    self._check_epoch(generation)
                    if (self.waiters[lane][0] is ticket and self.provider_waiters[0] is ticket and
                            not self.dedicated_active[lane] and
                            self.active + sum(self.dedicated_active.values()) < self.limit):
                        self.dedicated_active[lane] = 1
                        self.waiters[lane].remove(ticket)
                        self.provider_waiters.remove(ticket)
                        acquired = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("API lane exhausted")
                    self.condition.wait(min(remaining, .05))
            self._validate_waiter(cancelled, validate)
            with self.condition:
                self._check_epoch(generation)
            yield
        finally:
            with self.condition:
                if ticket in self.waiters[lane]:
                    self.waiters[lane].remove(ticket)
                if ticket in self.provider_waiters:
                    self.provider_waiters.remove(ticket)
                if acquired:
                    self.dedicated_active[lane] = 0
                self.condition.notify_all()
    def note_rate_limited(self):
        """Step the effective limit down after repeated provider rate limits."""
        with self.condition:
            self.clean_streak = 0
            if self.limit <= 1:
                self.rate_limit_streak = 0
                return self.limit
            self.rate_limit_streak += 1
            if self.rate_limit_streak < API_RATE_LIMIT_STREAK:
                return self.limit
            self.rate_limit_streak = 0
            self.limit -= 1
            self.downshifts += 1
            self.condition.notify_all()
            return self.limit

    def note_success(self):
        """Step back up after a run of clean turns, never above the configured ceiling."""
        with self.condition:
            self.rate_limit_streak = 0
            if self.limit >= self.maximum:
                self.clean_streak = 0
                return self.limit
            self.clean_streak += 1
            if self.clean_streak < API_RATE_LIMIT_RECOVER:
                return self.limit
            self.clean_streak = 0
            self.limit += 1
            self.condition.notify_all()
            return self.limit

    def invalidate(self):
        """Cancel in-flight provider turns on every analyzer (model source switch)."""
        with self.condition:
            self.generation += 1
            analyzers = [analyzer for analyzer, _ in self.entries]
            self.condition.notify_all()
        seen = set()
        for analyzer in analyzers:
            if id(analyzer) in seen:
                continue
            seen.add(id(analyzer))
            cancel = getattr(analyzer, "cancel", None)
            if callable(cancel):
                cancel()
        return seen

    def close_extras(self):
        """Close every analyzer except the primary; the caller owns the primary."""
        with self.condition:
            self.closed = True
            self.generation += 1
            kept = self.primary()
            extras = [analyzer for analyzer, _ in self.entries[1:] if analyzer is not kept]
            if not self.active:
                self.entries = [(kept, False)]
            self.condition.notify_all()
        for analyzer in extras:
            close = getattr(analyzer, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

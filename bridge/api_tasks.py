"""API task lifecycle ownership: one lock/condition, three registries, inflight count.

The coordinator owns scheduling state only. It never holds a reference to ``Backend``;
callers pass explicit keys, jobs and small callbacks. Model calls, history planning and
SQLite transactions stay in the service. Importing this module has no runtime side effects.
"""
from __future__ import annotations

import time


class ApiTaskCoordinator:
    """One RLock + Condition guarding the insight/portrait/inventory registries.

    ``model_source_revision`` is intentionally NOT owned here: it is a model-selection
    optimistic-concurrency version, never an API cancellation epoch.
    """

    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.inflight = 0
        self.insight_jobs = {}
        self.portrait_jobs = {}
        # key = (account, workdir, user, subject, highwater)
        self.inventory_jobs = {}
        # One short-lived, bounded handoff; never persists raw messages.
        self.inventory_pieces = None

    def begin(self):
        with self.condition:
            self.inflight += 1

    def finish(self):
        with self.condition:
            self.inflight -= 1
            self.condition.notify_all()

    def wait_for_idle(self, timeout):
        """Wait until every registered API worker has finished; True when idle."""
        with self.condition:
            return self.condition.wait_for(lambda: self.inflight == 0, timeout=timeout)

    def notify(self):
        with self.condition:
            self.condition.notify_all()

    def job_identity(self, registry, key):
        """The exact job object for ``key`` (or None); A->B->A must match identity."""
        return registry.get(key)

    def invalidate_models(self):
        """Stop insight and portrait jobs only; inventory may be reused across providers."""
        with self.condition:
            self.insight_jobs.clear()
            self.portrait_jobs.clear()
            self.condition.notify_all()

    def invalidate_source(self, account, source_id):
        """Drop only this account+source's model jobs; never touches the inventory."""
        with self.condition:
            for key in list(self.insight_jobs):
                if key[0] == account and key[2] == source_id:
                    del self.insight_jobs[key]
            for key in list(self.portrait_jobs):
                if key[0] == account and key[2] == source_id:
                    del self.portrait_jobs[key]
            self.condition.notify_all()

    def drop_portrait_error(self, source_id):
        """On a context-budget change, drop this source's errored portrait job."""
        with self.condition:
            for job_key, job in list(self.portrait_jobs.items()):
                if job_key[2] == source_id and job.get("status") == "error":
                    del self.portrait_jobs[job_key]

    def clear_insight_jobs(self):
        with self.condition:
            self.insight_jobs.clear()

    def drop_inventory_pieces_for(self, account):
        with self.condition:
            snapshot = self.inventory_pieces
            if snapshot is not None and snapshot[0][0] == account:
                self.inventory_pieces = None
            return snapshot

    def inventory_status(self, key, now):
        """Return (status, expired_removed); a timed tuple is treated as expired."""
        with self.condition:
            status = self.inventory_jobs.get(key)
            if isinstance(status, tuple):
                if status[1] <= now:
                    self.inventory_jobs.pop(key, None)
                    return None
                return status[0]
            return status

    def register_inventory(self, key):
        with self.condition:
            self.inventory_jobs[key] = "running"

    def finish_inventory(self, key):
        with self.condition:
            self.inventory_jobs.pop(key, None)
            self.condition.notify_all()

    def error_inventory(self, key, until):
        with self.condition:
            self.inventory_jobs[key] = ("error", until)
            self.condition.notify_all()

    def has_running_inventory(self):
        with self.condition:
            return any(state == "running" for state in self.inventory_jobs.values())

    def wait_api_model_retry(self, jobs, job_key, job, code, attempt, retry_seconds,
                             retry_max, check):
        """Expose one retryable error and wait; ``check`` re-validates source/registry/scope.

        ``retry_seconds``/``retry_max`` are passed per call so tests can patch the
        constants without freezing them at construction. The condition lock is released
        while waiting, and UI fields keep their exact shape.
        """
        with self.condition:
            deadline = time.monotonic() + retry_seconds
            job["retry"] = {"reason": code, "attempt": attempt, "max": retry_max,
                            "nextAtMs": int((time.time() + retry_seconds) * 1000)}
            self.condition.notify_all()
            while True:
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    job.pop("retry", None)
                    return
                self.condition.wait(timeout=min(1, remaining))

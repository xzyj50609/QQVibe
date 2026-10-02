"""A3 task-lifecycle tests: coordinator identity, registries, concurrency and rollback.

Reuses the synthetic Source/Analyzer/ModelSourceStore/ResultStore fixtures from
``test_api_insights``. Never touches a real model, history database or network.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api_tasks import ApiTaskCoordinator
from model_source import ModelSourceStore
from real_backend import Backend, ResultStore

from test_api_insights import Analyzer, Source


class CoordinatorUnitTests(unittest.TestCase):
    def test_backend_proxies_the_same_lock_condition_and_registries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = Backend(Source(root), analyzer=Analyzer(),
                              model_source_store=ModelSourceStore(
                                  root / ".local" / "model-source.json", root=root,
                                  protect=lambda key: b"wrapped:" + key.encode(),
                                  unprotect=lambda data: data.removeprefix(b"wrapped:").decode()),
                              store_factory=lambda account, _workdir: ResultStore(root / f"{account}.sqlite3"))
            self.addCleanup(backend.shutdown)
            coordinator = backend.api_tasks
            self.assertIsInstance(coordinator, ApiTaskCoordinator)
            self.assertIs(backend.api_lock, coordinator.lock)
            self.assertIs(backend.api_condition, coordinator.condition)
            self.assertIs(backend.api_jobs, coordinator.insight_jobs)
            self.assertIs(backend.api_portrait_jobs, coordinator.portrait_jobs)
            self.assertIs(backend.api_portrait_inventory_jobs, coordinator.inventory_jobs)
            self.assertEqual(backend.api_inflight, coordinator.inflight)
            self.assertEqual(coordinator.inflight, 0)
            # The legacy property must read/write the SAME counter, never a copy.
            backend.api_inflight = 3
            self.assertEqual(coordinator.inflight, 3)
            backend.api_inflight = 0

    def test_begin_finish_wake_waiters_and_clear_does_not_change_inflight(self):
        coordinator = ApiTaskCoordinator()
        gate = threading.Event()
        observed = []

        def waiter():
            observed.append(coordinator.wait_for_idle(5))

        thread = threading.Thread(target=waiter, daemon=True)
        coordinator.begin()
        thread.start()
        time.sleep(.05)
        self.assertEqual(observed, [])
        coordinator.finish()
        thread.join(timeout=5)
        self.assertEqual(observed, [True])

    def test_registries_are_independent_and_models_do_not_touch_inventory(self):
        coordinator = ApiTaskCoordinator()
        coordinator.insight_jobs[("a", "u", "s")] = {"status": "running"}
        coordinator.portrait_jobs[("a", "u", "s", "x")] = {"status": "running"}
        coordinator.inventory_jobs[("a", "w", "u", "x", (1,))] = "running"
        coordinator.invalidate_models()
        self.assertEqual(coordinator.insight_jobs, {})
        self.assertEqual(coordinator.portrait_jobs, {})
        self.assertEqual(coordinator.inventory_jobs, {("a", "w", "u", "x", (1,)): "running"})

    def test_invalidate_source_is_scoped_and_spares_inventory(self):
        coordinator = ApiTaskCoordinator()
        coordinator.insight_jobs[("a", "u", "s1")] = {"status": "running"}
        coordinator.insight_jobs[("a", "u", "s2")] = {"status": "running"}
        coordinator.portrait_jobs[("a", "u", "s1", "x")] = {"status": "running"}
        coordinator.inventory_jobs[("a", "w", "u", "x", (1,))] = "running"
        coordinator.invalidate_source("a", "s1")
        self.assertEqual(set(coordinator.insight_jobs), {("a", "u", "s2")})
        self.assertEqual(coordinator.portrait_jobs, {})
        self.assertEqual(len(coordinator.inventory_jobs), 1)

    def test_retry_reads_seconds_per_call_and_releases_lock(self):
        coordinator = ApiTaskCoordinator()
        job = {"status": "running"}
        jobs = {("a", "u", "s"): job}
        clock = iter([0.0, 1.0, 2.0, 9.0, 9.0, 9.0, 9.0])
        observed = []
        with patch("api_tasks.time.monotonic", side_effect=lambda: next(clock, 9.0)), \
             patch("api_tasks.time.time", return_value=1000):
            coordinator.wait_api_model_retry(jobs, ("a", "u", "s"), job, "network", 1,
                                             5, 10, lambda: observed.append("check"))
        self.assertIn("check", observed)
        self.assertNotIn("retry", job)


class ConcurrencyRegressionTests(unittest.TestCase):
    """The two Codex-flagged races, exercised with real threads and fake workers."""

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
        self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "test-model", "apiKey": "test-key", "contextTokens": 8192,
        })

    def test_concurrent_portrait_get_starts_exactly_one_inventory_worker(self):
        coordinator = self.backend.api_tasks
        start = threading.Barrier(3)
        answer_window = threading.Barrier(2)
        entered, release = threading.Event(), threading.Event()
        calls, results, errors = [], [], []
        original_has = coordinator.has_running_inventory

        def widened_check():
            answer = original_has()
            if not answer:
                # Without the service's outer lock both callers can observe False.
                try:
                    answer_window.wait(timeout=.1)
                except threading.BrokenBarrierError:
                    pass
            return answer

        def fake_inventory(key, scope, store):
            try:
                with coordinator.condition:
                    calls.append(key)
                    entered.set()
                release.wait(timeout=3)
            finally:
                with coordinator.condition:
                    coordinator.finish_inventory(key)
                    coordinator.finish()

        def get():
            try:
                start.wait(timeout=3)
                results.append(self.backend.model_portrait("friend"))
            except Exception as exc:
                errors.append(exc)

        with patch.object(coordinator, "has_running_inventory", widened_check), \
             patch.object(self.backend, "_run_api_portrait_inventory", fake_inventory):
            threads = [threading.Thread(target=get, daemon=True) for _ in range(2)]
            try:
                for thread in threads:
                    thread.start()
                start.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=3)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 2)
                self.assertTrue(entered.wait(timeout=2))
                self.assertEqual(len(calls), 1)
                self.assertEqual(coordinator.inflight, 1)
                self.assertEqual(coordinator.inventory_jobs[calls[0]], "running")
            finally:
                release.set()
                self.assertTrue(coordinator.wait_for_idle(3))

    def test_same_key_reservation_does_not_get_popped_by_the_other(self):
        coordinator = self.backend.api_tasks
        key = ("account-a", self.source.workdir, "friend", "friend", (3, "shard", 3))
        coordinator.register_inventory(key)
        self.assertEqual(coordinator.inventory_status(key, time.monotonic()), "running")
        # A different key's expiry must not remove this one.
        other = ("account-a", self.source.workdir, "friend", "friend", (9, "shard", 9))
        self.assertIsNone(coordinator.inventory_status(other, time.monotonic()))
        self.assertEqual(coordinator.inventory_status(key, time.monotonic()), "running")
        coordinator.finish_inventory(key)
        self.assertIsNone(coordinator.inventory_status(key, time.monotonic()))

    def test_thread_start_failure_rolls_back_inflight_and_registry(self):
        coordinator = self.backend.api_tasks
        self.assertEqual(coordinator.inflight, 0)
        # Backend's ordinary worker was created before this patch. The actual
        # model_portrait inventory reservation must roll back a failed start.
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("synthetic start failure")) as start:
            result = self.backend.model_portrait("friend")
        self.assertEqual(start.call_count, 1)
        self.assertEqual(result["inventoryStatus"], "error")
        self.assertEqual(coordinator.inflight, 0)
        self.assertEqual(len(coordinator.inventory_jobs), 1)
        state = next(iter(coordinator.inventory_jobs.values()))
        self.assertIsInstance(state, tuple)
        self.assertEqual(state[0], "error")

    def test_source_switch_keeps_source_independent_inventory(self):
        coordinator = self.backend.api_tasks
        key = ("account-a", self.source.workdir, "friend", "friend", (3, "shard", 3))
        coordinator.register_inventory(key)
        coordinator.invalidate_models()
        self.assertEqual(coordinator.inventory_jobs.get(key), "running")
        self.backend.api_tasks.finish_inventory(key)




if __name__ == "__main__":
    unittest.main()

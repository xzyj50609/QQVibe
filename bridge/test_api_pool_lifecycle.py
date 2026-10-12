"""Synthetic waiting/cancellation/shutdown races; no provider requests or processes."""
import json
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from test_api_workers import make_pool


class PoolLifecycleTests(unittest.TestCase):
    def pool(self, workers=1):
        pool, primary, created, temporary = make_pool(workers)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(pool.close_extras)
        return pool

    def waiter(self, pool, *, cancelled=None, lane=None):
        checked = threading.Event()
        calls, errors = [], []
        def run():
            try:
                context = (pool.dedicated(lane, cancelled=cancelled, validate=checked.set)
                           if lane else pool.lease(cancelled=cancelled, validate=checked.set))
                with context:
                    calls.append("provider")
            except Exception as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(checked.wait(2))
        self.addCleanup(thread.join, 2)
        return thread, calls, errors

    def test_cancelled_waiter_never_reaches_provider_after_capacity_returns(self):
        pool, cancelled = self.pool(), threading.Event()
        with pool.lease():
            thread, calls, errors = self.waiter(pool, cancelled=cancelled.is_set)
            cancelled.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])
        self.assertEqual(errors, ["model-source-changed"])
        self.assertEqual(pool.status()["waiting"], 0)

    def test_source_invalidation_cancels_old_waiters_but_allows_new_requests(self):
        pool = self.pool()
        with pool.lease():
            thread, calls, errors = self.waiter(pool)
            pool.invalidate()
        thread.join(2)
        self.assertEqual(calls, [])
        self.assertEqual(errors, ["model-source-changed"])
        with pool.lease():
            self.assertEqual(pool.status()["active"], 1)

    def test_post_acquisition_validation_prevents_provider_and_releases_capacity(self):
        pool = self.pool()
        checks = []
        def validate():
            checks.append(True)
            if len(checks) == 2:
                raise RuntimeError("synthetic-reset")
        with self.assertRaisesRegex(RuntimeError, "synthetic-reset"):
            with pool.lease(validate=validate):
                self.fail("provider must not run")
        self.assertEqual(pool.status()["active"], 0)

    def test_shutdown_does_not_reindex_leases_or_restart_waiters(self):
        pool = self.pool(2)
        with pool.lease(), pool.lease():
            thread, calls, errors = self.waiter(pool)
            pool.close_extras()
        thread.join(2)
        self.assertEqual(calls, [])
        self.assertEqual(errors, ["model-source-changed"])
        self.assertEqual(pool.status()["active"], 0)
        with self.assertRaisesRegex(RuntimeError, "model-source-changed"):
            with pool.lease():
                self.fail("closed pool restarted")

    def test_dedicated_lanes_share_a_declared_total_envelope_and_cancel_waiters(self):
        pool = self.pool(4)
        with pool.lease(), pool.lease(), pool.dedicated("portrait"), pool.dedicated("probe"):
            status = pool.status()
            self.assertEqual((status["totalActive"], status["totalLimit"]), (4, 4))
            thread, calls, errors = self.waiter(pool, lane="portrait")
            pool.invalidate()
        thread.join(2)
        self.assertEqual(calls, [])
        self.assertEqual(errors, ["model-source-changed"])
        self.assertEqual(pool.status()["totalActive"], 0)

    def test_resize_does_not_oversubscribe_when_high_numbered_slots_are_still_busy(self):
        pool = self.pool(3)
        first, remaining = ExitStack(), ExitStack()
        with first, remaining:
            first.enter_context(pool.lease())
            remaining.enter_context(pool.lease())
            remaining.enter_context(pool.lease())
            pool.configure(1)
            first.close()
            thread, calls, errors = self.waiter(pool)
            self.assertEqual(pool.status()["active"], 2)
            self.assertEqual(calls, [])
        thread.join(2)
        self.assertEqual((calls, errors), (["provider"], []))

    def test_failed_settings_write_does_not_partially_resize(self):
        pool = self.pool(2)
        with patch("api_pool.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                pool.configure(4)
        self.assertEqual(pool.status()["limit"], 2)
        with open(pool.settings_path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"workers": 2})


if __name__ == "__main__":
    unittest.main()

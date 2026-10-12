"""API parallelism: pool leases, rate-limit downshift, and the HTTP contract.

The pool never launches a real Node process here: every test injects a factory that
returns a stub, so the assertions are about scheduling and bookkeeping only.
"""
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from api_pool import (API_RATE_LIMIT_RECOVER, API_RATE_LIMIT_STREAK, ApiAnalyzerPool,
                      DEFAULT_API_WORKERS, MAX_API_WORKERS, api_worker_settings)
from real_http import make_handler


class FakeAnalyzer:
    def __init__(self):
        self.cancelled = 0
        self.closed = 0

    def cancel(self):
        self.cancelled += 1

    def close(self):
        self.closed += 1


def make_pool(workers):
    """A pool whose primary is stable and whose extras are recorded, never launched."""
    created = []
    primary = FakeAnalyzer()
    temporary = tempfile.TemporaryDirectory()

    def factory():
        created.append(FakeAnalyzer())
        return created[-1]

    pool = ApiAnalyzerPool(lambda: primary, factory=factory,
                           settings_path=Path(temporary.name) / "api-workers.json")
    pool.configure(workers)
    return pool, primary, created, temporary


class ApiPoolTests(unittest.TestCase):
    def test_default_is_one_worker(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(api_worker_settings(Path(temporary.name) / "missing.json")["workers"],
                         DEFAULT_API_WORKERS)
        self.assertEqual(MAX_API_WORKERS, 4)

    def test_ceiling_is_clamped_to_the_supported_range(self):
        pool, _, _, temporary = make_pool(99)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(pool.status()["workers"], MAX_API_WORKERS)
        pool.configure(0)
        self.assertEqual(pool.status()["workers"], 1)

    def test_one_lease_per_analyzer_and_spawning_only_under_pressure(self):
        pool, primary, created, temporary = make_pool(3)
        self.addCleanup(temporary.cleanup)
        with pool.lease() as first:
            self.assertIs(first, primary)
            self.assertEqual(created, [], "an unused ceiling must not start a process")
            self.assertEqual(pool.status()["active"], 1)
        pool.configure(1)
        with pool.lease() as only:
            self.assertIs(only, primary)

    def test_leases_run_concurrently_up_to_the_ceiling(self):
        pool, _, created, temporary = make_pool(3)
        self.addCleanup(temporary.cleanup)
        start = threading.Barrier(3, timeout=5)
        release = threading.Event()
        release.set()
        seen, errors = [], []

        def worker():
            try:
                with pool.lease() as analyzer:
                    seen.append(analyzer)
                    start.wait()
                    release.wait(5)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(seen), 3)
        self.assertEqual(len({id(analyzer) for analyzer in seen}), 3,
                         "each concurrent turn needs its own analyzer")
        self.assertEqual(len(created), 2, "two extra processes for three concurrent turns")
        self.assertEqual(pool.status()["active"], 0)

    def test_rate_limits_step_the_limit_down_and_clean_turns_step_it_back(self):
        pool, _, _, temporary = make_pool(3)
        self.addCleanup(temporary.cleanup)
        for _ in range(API_RATE_LIMIT_STREAK):
            pool.note_rate_limited()
        self.assertEqual(pool.status()["limit"], 2)
        self.assertEqual(pool.status()["downshifts"], 1)
        for _ in range(API_RATE_LIMIT_RECOVER):
            pool.note_success()
        self.assertEqual(pool.status()["limit"], 3)
        self.assertEqual(pool.status()["downshifts"], 1, "recovery is not a downshift")

    def test_downshift_never_falls_below_one_nor_exceeds_the_ceiling(self):
        pool, _, _, temporary = make_pool(2)
        self.addCleanup(temporary.cleanup)
        for _ in range(10):
            pool.note_rate_limited()
        self.assertEqual(pool.status()["limit"], 1)
        for _ in range(API_RATE_LIMIT_RECOVER * 3):
            pool.note_success()
        self.assertEqual(pool.status()["limit"], 2)

    def test_downshifted_pool_stops_handing_out_extra_slots(self):
        pool, primary, created, temporary = make_pool(3)
        self.addCleanup(temporary.cleanup)
        for _ in range(API_RATE_LIMIT_STREAK):
            pool.note_rate_limited()
        with pool.lease() as analyzer:
            self.assertIs(analyzer, primary)
        self.assertEqual(created, [])

    def test_invalidate_and_close_extras_touch_every_analyzer_once(self):
        pool, primary, created, temporary = make_pool(2)
        self.addCleanup(temporary.cleanup)
        # Two simultaneous turns are what forces a second process; sequential turns
        # reuse slot 0.
        with pool.lease():
            with pool.lease() as second:
                extra = second
        self.assertEqual(len(created), 1)
        self.assertIsNot(extra, primary)
        pool.invalidate()
        pool.close_extras()
        self.assertEqual((extra.cancelled, extra.closed), (1, 1))
        self.assertEqual(primary.cancelled, 1)
        self.assertEqual(primary.closed, 0, "the primary is closed by the backend, not the pool")
        self.assertEqual(pool.status()["spawned"], 0)

    def test_settings_file_persists_the_ceiling(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "api-workers.json"
        pool = ApiAnalyzerPool(FakeAnalyzer(), factory=FakeAnalyzer, settings_path=path)
        pool.configure(4)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["workers"], 4)
        self.assertEqual(api_worker_settings(path)["workers"], 4)
class ApiWorkerHttpTests(unittest.TestCase):
    """The settings row reaches the bridge over the same loopback transport."""

    def setUp(self):
        from real_backend import Backend
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = object.__new__(Backend)
        self.backend.analyzer = FakeAnalyzer()
        self.backend.api_analyzer = self.backend.analyzer
        self.backend.api_portrait_analyzer = self.backend.analyzer
        self.backend.api_probe_analyzer = self.backend.analyzer
        self.backend.api_pool = ApiAnalyzerPool(
            lambda: self.backend.api_analyzer, factory=FakeAnalyzer,
            settings_path=Path(self.temp.name) / "api-workers.json")
        # The transport takes a real lease; a hand-built Backend has no counters.
        self.backend.request_lease = nullcontext
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server

    def call(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
        finally:
            connection.close()

    def test_get_reports_the_current_ceiling(self):
        status, payload = self.call("GET", "/api/api-workers")
        self.assertEqual(status, 200)
        self.assertEqual(payload["workers"], 1)
        self.assertEqual(payload["max"], MAX_API_WORKERS)
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["active"], 0)

    def test_post_resizes_and_persists(self):
        status, payload = self.call("POST", "/api/api-workers", {"workers": 3})
        self.assertEqual(status, 200)
        self.assertEqual(payload["workers"], 3)
        path = Path(self.temp.name) / "api-workers.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["workers"], 3)
        self.assertEqual(self.call("GET", "/api/api-workers")[1]["workers"], 3)

    def test_post_rejects_out_of_range_and_wrong_types(self):
        for body in ({"workers": 0}, {"workers": 5}, {"workers": "3"}, {"workers": 2.5},
                     {"workers": True}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/api-workers", body)[0], 400)
        self.assertEqual(self.call("GET", "/api/api-workers")[1]["workers"], 1)


if __name__ == "__main__":
    unittest.main()

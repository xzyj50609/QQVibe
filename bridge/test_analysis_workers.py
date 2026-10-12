"""Local analysis worker ceiling: status, validated edits and persistence."""

import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from conversation_selection import ConversationSelectionStore
from real_backend import Backend
from real_http import make_handler


class StubSource:
    def __init__(self, workdir):
        self.workdir = workdir
        self.account = "wxid_real_a_abcd"

    def verified_identity(self, *, messages=False):
        return self.account, self.workdir

    def sessions(self):
        return {"account": self.account, "sessions": [], "messagesReady": False}


class StubAnalyzer:
    model = {"state": "ready"}


class AnalysisWorkerSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with mock.patch("backend_service.save_worker_settings"):
            self.backend = Backend(StubSource(root / "snapshot"), StubAnalyzer(),
                                   selection_store=ConversationSelectionStore(root / "results"))
        self.addCleanup(self.backend.shutdown)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, body=None):
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, "/api/analysis-workers", body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_status_reports_the_configured_ceiling_and_limit(self):
        status, data = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(data["workers"], 1)
        self.assertEqual(data["max"], 4)
        self.assertFalse(data["elastic"])
        self.assertEqual(data["limit"], 1)
        self.assertIn("gpu", data)

    def test_edits_are_validated_and_the_choice_is_persisted(self):
        for body in ({"workers": 0}, {"workers": 9}, {"workers": "2"},
                     {"workers": True}, {"elastic": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", body)[0], 400)
        with mock.patch("backend_service.save_worker_settings") as saved:
            status, data = self.request("POST", {"workers": 1, "elastic": False})
            self.assertEqual(status, 200)
            self.assertEqual(data["workers"], 1)
            saved.assert_called_once_with(1, False)



class ElasticWorkerTests(unittest.TestCase):
    """The review of the parallel-workers PR found four defects; each test pins one."""

    def _elastic_backend(self, workers=2):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with mock.patch("backend_service.save_worker_settings"):
            backend = Backend(StubSource(root / "snapshot"), StubAnalyzer(),
                              selection_store=ConversationSelectionStore(root / "results"))
        self.addCleanup(self._drain, backend)
        backend._stop_workers()
        backend._analyzers = [StubAnalyzer() for _ in range(workers)]
        backend.worker_count = workers
        backend.elastic_workers = True
        backend.worker_limit = workers
        # These tests drive the limit themselves: a live sampler would ramp it back up, and a
        # calm machine legitimately restores the ceiling after two samples.
        backend._apply_load_sample = lambda sample: None
        backend._sample_load = self._calm
        return backend

    def _drain(self, backend):
        try:
            backend.closing = True
            backend._stop_workers()
        except Exception:
            pass

    @staticmethod
    def _calm():
        # A fixed sample keeps the monitor off psutil and nvidia-smi inside the unit tests.
        return {"cpu": 1.0, "ownCpu": 1.0, "otherCpu": 0.0, "memory": 10.0,
                "gpu": 0.0, "gpuFreeMiB": 8000.0}

    def test_the_gpu_probe_actually_reaches_nvidia_smi(self):
        # `subprocess` was never imported, so the probe raised NameError, the broad except
        # swallowed it, and the sampler saw a machine that was always calm.
        backend = self._elastic_backend()
        probe = mock.Mock(returncode=0, stdout="82, 4700, 8188\n", stderr="")
        with mock.patch("subprocess.run", return_value=probe) as run:
            sample = Backend.__dict__["_sample_load"](backend)
        self.assertTrue(run.called)
        self.assertEqual(sample["gpu"], 82.0)
        self.assertEqual(sample["gpuFreeMiB"], 3488.0)

    def test_own_cpu_keeps_the_same_process_objects(self):
        # cpu_percent(interval=None) is a delta on the same Process object; rebuilding it
        # every sample made our own load read as 0.0 forever.
        backend = self._elastic_backend()
        process = mock.Mock(pid=4242)
        process.children.return_value = []
        process.cpu_percent.return_value = 7.0
        psutil = mock.Mock()
        psutil.Process.return_value = process
        self.assertEqual(backend._own_cpu_percent(psutil), 7.0)
        self.assertEqual(backend._own_cpu_percent(psutil), 7.0)
        self.assertEqual(psutil.Process.call_count, 1)

    def test_a_task_waits_instead_of_starting_after_the_limit_drops(self):
        # Both workers are already blocked on the queue when the limit drops to zero. Without
        # the requeue they still took the next task, so the pool kept starting inferences
        # while the machine had asked it to stand down.
        backend = self._elastic_backend(workers=2)
        backend._start_workers()
        time.sleep(0.3)
        backend._set_worker_limit(0)
        backend.tasks.put((1, next(backend.task_serial), ("session-key",)))
        time.sleep(0.4)
        self.assertEqual(backend.tasks.qsize(), 1, "a parked worker took the task")
        backend.tasks.get()
        backend.tasks.task_done()

    def test_the_sampler_restarts_after_a_drain(self):
        # The monitor returns when the bridge closes; leaving the handle set meant an account
        # clear could never bring elastic mode back.
        backend = self._elastic_backend()
        backend._start_workers()
        first = backend.load_monitor_thread
        self.assertIsNotNone(first)
        backend.closing = True
        backend._stop_workers()
        self.assertIsNone(backend.load_monitor_thread)
        backend.closing = False
        backend._ensure_load_monitor()
        second = backend.load_monitor_thread
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)
        backend.closing = True

    def test_a_lowered_fixed_count_parks_the_extra_worker(self):
        # Without elastic mode the parking branch was skipped, so lowering the fixed count
        # only released the extra model, which relaunched on the next task.
        backend = self._elastic_backend(workers=2)
        backend.elastic_workers = False
        ran = []

        def run_one(task):
            # Long enough that an idle second worker would get its turn at the queue.
            ran.append(backend._analyzer_ctx.index)
            time.sleep(0.05)
            backend.tasks.task_done()

        backend._run_one = run_one
        backend._start_workers()
        with mock.patch("backend_service.save_worker_settings"):
            backend.set_worker_settings(1, False)
        time.sleep(0.3)
        for number in range(6):
            backend.tasks.put((1, next(backend.task_serial), (f"session-{number}",)))
        backend.tasks.join()
        self.assertEqual(set(ran), {0})

    def test_turning_follow_load_off_stops_the_sampler(self):
        # A running sampler kept moving the limit after "follow load" was switched off,
        # overriding the fixed count the user had just chosen.
        backend = self._elastic_backend(workers=2)
        backend._start_workers()
        monitor = backend.load_monitor_thread
        self.assertIsNotNone(monitor)
        with mock.patch("backend_service.save_worker_settings"):
            status = backend.set_worker_settings(2, False)
        monitor.join(timeout=2)
        self.assertFalse(monitor.is_alive())
        self.assertEqual(status["limit"], 2)

if __name__ == "__main__":
    unittest.main()

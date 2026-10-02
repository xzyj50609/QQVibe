"""Synthetic launcher checks; never starts the project bridge or a browser."""

import importlib.util
import io
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("start-real-client.py")
spec = importlib.util.spec_from_file_location("real_client_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = launcher
spec.loader.exec_module(launcher)


class HealthHandler(BaseHTTPRequestHandler):
    version = "real-ui-1"
    instance_id = None
    app_version = None

    def do_GET(self):
        body = json.dumps({"version": self.version, "instanceId": self.instance_id,
                           "appVersion": self.app_version}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.control_tokens.append(self.headers.get(launcher.CONTROL_TOKEN_HEADER))
        body = json.dumps({"stopping": True}).encode()
        self.send_response(202)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeProcess:
    pid = 47231
    returncode = None

    def poll(self):
        return None


def unused_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="client fixture ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文 空格"
        (self.root / "bridge").mkdir(parents=True)
        (self.root / "bridge" / "chat_server.py").write_text("fixture only", encoding="utf-8")
        self.config = launcher.Config(self.root, Path(sys.executable), unused_port(), 0.35)

    def server(self, version, instance_id="matching", app_version=None):
        identity = self.config.instance_id if instance_id == "matching" else instance_id
        handler = type("FixtureHandler", (HealthHandler,), {"version": version,
            "instance_id": identity, "app_version": app_version})
        server = ThreadingHTTPServer(("127.0.0.1", self.config.port), handler)
        server.control_tokens = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_reuses_matching_version_without_creating_runtime_files(self):
        self.server("real-ui-1")
        self.assertEqual(launcher.ensure_service(self.config, starter=lambda *_: self.fail("started")), (False, None))
        self.assertFalse(self.config.runtime_dir.exists())

    def test_rejects_wrong_version_and_non_http_occupant(self):
        self.server("old-version")
        with self.assertRaisesRegex(launcher.LauncherError, "occupied"):
            launcher.ensure_service(self.config, starter=lambda *_: self.fail("started"))
        self.assertFalse(self.config.runtime_dir.exists())

    def test_rejects_same_version_from_another_installation_or_legacy_bridge(self):
        self.server("real-ui-1", "0" * 64)
        self.assertIn("installation identity differs", launcher.health(self.config))
        with self.assertRaisesRegex(launcher.LauncherError, "occupied"):
            launcher.ensure_service(self.config, starter=lambda *_: self.fail("started"))
        self.assertFalse(self.config.runtime_dir.exists())

    def test_rejects_same_installation_bridge_from_another_app_version(self):
        (self.root / "package.json").write_text('{"version":"1.0.2"}', encoding="utf-8")
        self.server("real-ui-1", app_version="1.0.1")
        self.assertIn("wrong service", launcher.health(self.config))
        with self.assertRaisesRegex(launcher.LauncherError, "occupied"):
            launcher.ensure_service(self.config, starter=lambda *_: self.fail("started"))

    def test_port_and_identity_are_stable_for_resolved_root(self):
        self.assertEqual(launcher.default_port(self.root), launcher.default_port(self.root / "."))
        self.assertEqual(launcher.instance_id(self.root), launcher.instance_id(self.root / "."))
        self.assertNotEqual(launcher.instance_id(self.root), launcher.instance_id(self.root.parent))
        self.assertTrue(20000 <= launcher.default_port(self.root) < 60000)

    def test_rejects_tcp_listener_without_health(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.config.port))
            listener.listen()
            with self.assertRaisesRegex(launcher.LauncherError, "occupied"):
                launcher.ensure_service(self.config, starter=lambda *_: self.fail("started"))

    def test_concurrent_launch_creates_once_and_records_unicode_path(self):
        calls = []
        outcomes = []
        errors = []
        barrier = threading.Barrier(2)
        server = None

        def starter(config, log_path, control_token):
            nonlocal server
            self.assertRegex(control_token, r"^[0-9a-f]{64}$")
            calls.append(log_path)
            log_path.write_text("synthetic bridge", encoding="utf-8")
            time.sleep(0.12)
            server = self.server("real-ui-1")
            return FakeProcess()

        def run():
            try:
                barrier.wait()
                outcomes.append(launcher.ensure_service(self.config, starter=starter))
            except Exception as error:
                errors.append(error)

        with patch.object(launcher, "process_identity", return_value=(True, 42)):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(created for created, _ in outcomes), [False, True])
        self.assertIsNotNone(server)
        metadata = json.loads((self.config.runtime_dir / "bridge-47231.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["pid"], 47231)
        self.assertIn("中文 空格", metadata["log"])
        self.assertRegex(metadata["control_token"], r"^[0-9a-f]{64}$")

    def test_timeout_reports_log_without_claiming_success(self):
        with patch.object(launcher, "process_identity", return_value=(True, 42)):
            with self.assertRaisesRegex(launcher.LauncherError, "did not report.*log:"):
                launcher.ensure_service(self.config, starter=lambda *_: FakeProcess())
        self.assertTrue((self.config.runtime_dir / "bridge-47231.json").is_file())

    def test_early_exit_reports_log(self):
        class ExitedProcess(FakeProcess):
            returncode = 19

            def poll(self):
                return self.returncode

        with patch.object(launcher, "process_identity", return_value=(True, 42)):
            with self.assertRaisesRegex(launcher.LauncherError, "exited with code 19; log:"):
                launcher.ensure_service(self.config, starter=lambda *_: ExitedProcess())

    def test_retry_after_timeout_does_not_spawn_second_live_bridge(self):
        calls = []

        def starter(*_):
            calls.append(True)
            return FakeProcess()

        with patch.object(launcher, "process_identity", return_value=(True, 42)):
            with self.assertRaisesRegex(launcher.LauncherError, "did not report"):
                launcher.ensure_service(self.config, starter=starter)
            with self.assertRaisesRegex(launcher.LauncherError, "no duplicate started; log:"):
                launcher.ensure_service(self.config, starter=starter)
        self.assertEqual(len(calls), 1)

    def test_dead_record_is_reclaimed_before_a_new_bridge_starts(self):
        stale = self.owned_record()
        cleanup = []
        config = launcher.Config(self.config.root, self.config.python_exe, self.config.port, 0.0)

        def reclaim(_config, record, **_kwargs):
            cleanup.append(record["pid"])
            return []

        launched_record = {"pid": 47231, "port": config.port, "instance_id": config.instance_id,
                          "created_filetime": 42, "control_token": "b" * 64}
        with patch.object(launcher, "process_identity", return_value=(False, None)), \
             patch.object(launcher, "abort_owned_launch", side_effect=reclaim), \
             patch.object(launcher, "write_control_record", return_value=launched_record), \
             patch.object(launcher, "clear_no_auto_recovery"), \
             patch.object(launcher.time, "sleep"):
            with self.assertRaisesRegex(launcher.LauncherError, "did not report"):
                launcher.ensure_service(config, starter=lambda *_: FakeProcess())
        self.assertEqual(cleanup, [47231, 47231])
        self.assertTrue(stale.exists(), "the test double leaves record deletion to the cleanup helper")


    def test_health_timeout_aborts_only_the_started_bridge_and_removes_record(self):
        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch.object(launcher, "bridge_exited", return_value=True), \
             patch.object(launcher, "orphan_analysis_children", return_value=[]), \
             patch.object(launcher, "finish_owned_shutdown") as finish:
            with self.assertRaisesRegex(launcher.LauncherError, "did not report.*log:"):
                launcher.ensure_service(self.config, starter=lambda *_: FakeProcess())
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(finish.call_args.args[1]["pid"], 47231)
        self.assertEqual(list(self.config.runtime_dir.glob("bridge-*.json")), [],
                         "the failed launch must not leave an orphan record")

    def test_early_exit_aborts_owned_tree_and_removes_record(self):
        class ExitedProcess(FakeProcess):
            returncode = 19

            def poll(self):
                return self.returncode

        worker = object()
        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch.object(launcher, "bridge_exited", return_value=True), \
             patch.object(launcher, "orphan_analysis_children", return_value=[worker]) as orphans, \
             patch.object(launcher, "finish_owned_shutdown") as finish:
            with self.assertRaisesRegex(launcher.LauncherError, "exited with code 19.*log:"):
                launcher.ensure_service(self.config, starter=lambda *_: ExitedProcess())
        self.assertEqual(orphans.call_count, 2,
                         "cleanup rechecks after the bridge exit race")
        self.assertEqual(finish.call_count, 2)
        self.assertEqual(finish.call_args_list[-1].args[2], [worker],
                         "workers of an early-exit bridge are reclaimed")
        self.assertEqual(list(self.config.runtime_dir.glob("bridge-*.json")), [])

    def test_parent_exit_race_rechecks_workers_after_initial_cleanup(self):
        late_worker = object()
        bridge_dead = False
        finish_children = []

        def exited(_record):
            return bridge_dead

        def finish(_config, _record, children, timeout=None):
            nonlocal bridge_dead
            finish_children.append(children)
            # Simulate the bridge dying after the first ownership check but before
            # the shutdown helper can inspect its children.
            if not children:
                bridge_dead = True

        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch.object(launcher, "bridge_exited", side_effect=exited), \
             patch.object(launcher, "orphan_analysis_children", return_value=[late_worker]), \
             patch.object(launcher, "finish_owned_shutdown", side_effect=finish):
            with self.assertRaisesRegex(launcher.LauncherError, "did not report.*log:"):
                launcher.ensure_service(self.config, starter=lambda *_: FakeProcess())

        self.assertEqual(finish_children, [[], [late_worker]],
                         "a worker created during parent exit must be reclaimed")
        self.assertEqual(list(self.config.runtime_dir.glob("bridge-*.json")), [])

    def test_orphan_worker_match_is_scoped_to_script_and_launch_window(self):
        from datetime import datetime, timezone
        from unittest.mock import Mock
        node = self.root / "node.exe"
        node.write_bytes(b"fixture")
        script = self.root / "bridge" / "analysis_server.ts"
        script.write_text("fixture only", encoding="utf-8")
        started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

        young = Mock()
        young.info = {"pid": 900, "name": "node.exe", "create_time": time.time() + 1}
        young.exe.return_value = str(node)
        young.cmdline.return_value = [str(node), "--import", "tsx", str(script)]
        old = Mock()
        old.info = {"pid": 901, "name": "node.exe", "create_time": time.time() - 3600}
        old.exe.return_value = str(node)
        old.cmdline.return_value = [str(node), "--import", "tsx", str(script)]
        foreign = Mock()
        foreign.info = {"pid": 902, "name": "node.exe", "create_time": time.time() + 1}
        foreign.exe.return_value = str(node)
        foreign.cmdline.return_value = [str(node), "--import", "tsx", str(self.root / "other.ts")]
        python_worker = Mock()
        python_worker.info = {"pid": 903, "name": "python.exe", "create_time": time.time() + 1}
        python_worker.exe.return_value = str(self.config.python_exe)
        python_worker.cmdline.return_value = [str(self.config.python_exe), str(script)]

        record = {"pid": 47231, "created_filetime": 42, "started_at": started}
        with patch("psutil.process_iter", return_value=[young, old, foreign, python_worker]):
            self.assertEqual(launcher.orphan_analysis_children(self.config, record), [young])

    def test_unreadable_recent_node_worker_blocks_false_cleanup_success(self):
        import psutil
        from datetime import datetime, timezone
        from unittest.mock import Mock
        started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        denied = Mock()
        denied.info = {"pid": 904, "name": "node.exe", "create_time": time.time() + 1}
        denied.cmdline.side_effect = psutil.AccessDenied()
        record = {"pid": 47231, "created_filetime": 42, "started_at": started}
        with patch("psutil.process_iter", return_value=[denied]):
            with self.assertRaisesRegex(launcher.LauncherError, "possible analysis worker PID 904"):
                launcher.orphan_analysis_children(self.config, record)

    def test_failed_cleanup_keeps_record_and_is_reported_not_silently_claimed(self):
        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch.object(launcher, "finish_owned_shutdown",
                          side_effect=launcher.LauncherError("Owned analysis worker did not exit")):
            with self.assertRaisesRegex(launcher.LauncherError,
                                        "did not report.*cleanup failed: Owned analysis worker did not exit"):
                launcher.ensure_service(self.config, starter=lambda *_: FakeProcess())
        self.assertTrue((self.config.runtime_dir / "bridge-47231.json").is_file(),
                        "an unverified cleanup must keep the record for a later retry")

    def test_wrong_service_after_start_never_contacts_a_foreign_service(self):
        def starter(*_):
            self.server("old-version")
            return FakeProcess()

        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch.object(launcher, "bridge_exited", return_value=True), \
             patch.object(launcher, "orphan_analysis_children", return_value=[]), \
             patch.object(launcher, "request_owned_shutdown") as request, \
             patch.object(launcher, "finish_owned_shutdown") as finish:
            with self.assertRaisesRegex(launcher.LauncherError, "different service"):
                launcher.ensure_service(self.config, starter=starter)
        self.assertEqual(finish.call_count, 1)
        request.assert_not_called()

    def owned_record(self, token="a" * 64, created=42):
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        record = {"pid": 47231, "port": self.config.port,
                  "instance_id": self.config.instance_id,
                  "created_filetime": created, "control_token": token}
        path = self.config.runtime_dir / "bridge-47231.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def isolated_shutdown_fixture(self, *, exit_delay):
        """Launch a data-free HTTP fixture under this test's temporary root."""
        script = self.root / "bridge" / "chat_server.py"
        script.write_text('''
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"version": "real-ui-1", "instanceId": os.environ["FIXTURE_INSTANCE_ID"]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # The launcher authenticates a stop with the QQ product's control header.
        if self.path != "/api/control/shutdown" or self.headers.get("X-QQVibe-Control-Token") != "a" * 64:
            self.send_error(403)
            return
        body = b'{"stopping":true}'
        self.send_response(202)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass

server = HTTPServer(("127.0.0.1", int(os.environ["CHATUI_PORT"])), Handler)
server.serve_forever()
server.server_close()
time.sleep(float(os.environ["FIXTURE_EXIT_DELAY"]))
''', encoding="utf-8")
        process = subprocess.Popen([str(self.config.python_exe), str(script)], cwd=self.root,
                                   env={**launcher.os.environ, "CHATUI_PORT": str(self.config.port),
                                        "FIXTURE_INSTANCE_ID": self.config.instance_id,
                                        "FIXTURE_EXIT_DELAY": str(exit_delay)},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while launcher.health(self.config) != "ready" and time.monotonic() < deadline:
            self.assertIsNone(process.poll(), "isolated fixture exited before health")
            time.sleep(0.05)
        self.assertEqual(launcher.health(self.config), "ready")
        alive, created = launcher.process_identity(process.pid)
        self.assertTrue(alive)
        self.assertIsInstance(created, int)
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.config.runtime_dir / f"bridge-{process.pid}.json").write_text(json.dumps({
            "pid": process.pid, "port": self.config.port, "instance_id": self.config.instance_id,
            "created_filetime": created, "control_token": "a" * 64,
        }), encoding="utf-8")
        return process

    def test_control_channel_names_are_the_qq_product(self):
        """A second product on this machine must not read or answer this token."""
        self.assertEqual(launcher.CONTROL_TOKEN_ENV, "QQVIBE_CONTROL_TOKEN")
        self.assertEqual(launcher.CONTROL_TOKEN_HEADER, "X-QQVibe-Control-Token")

    def test_start_passes_control_token_only_to_bridge_environment(self):
        log = self.root / "bridge.log"
        with patch.object(launcher.subprocess, "Popen") as start:
            launcher.start_service(self.config, log, "a" * 64)
        args, kwargs = start.call_args
        self.assertEqual(args[0][1], str(self.root / "bridge" / "chat_server.py"))
        self.assertEqual(kwargs["env"][launcher.CONTROL_TOKEN_ENV], "a" * 64)
        self.assertNotIn(launcher.CONTROL_TOKEN_ENV, launcher.os.environ)

    def test_record_failure_stops_only_new_child_and_leaves_no_token_record(self):
        class NewChild(FakeProcess):
            def __init__(self):
                self.terminated = False
                self.waited = False

            def poll(self):
                return 1 if self.terminated else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                self.waited = True
                self.wait_timeout = timeout
                return 1

        for identity, failure in (((True, 42), "ACL unavailable"),
                                  ((False, None), "creation time")):
            with self.subTest(failure=failure):
                child = NewChild()
                with patch.object(launcher, "process_identity", return_value=identity), \
                     patch.object(launcher, "protect_record", side_effect=launcher.LauncherError("ACL unavailable")):
                    with self.assertRaisesRegex(launcher.LauncherError, failure):
                        launcher.ensure_service(self.config, starter=lambda *_: child)
                self.assertTrue(child.terminated)
                self.assertTrue(child.waited)
                self.assertEqual(child.wait_timeout, 5)
                self.assertEqual(list(self.config.runtime_dir.glob("bridge-*.json")), [])
                self.assertEqual(list(self.config.runtime_dir.glob("*.tmp")), [])

    def test_stop_owned_bridge_requires_token_and_exact_process_ownership(self):
        server = self.server("real-ui-1")
        self.owned_record()
        with patch.object(launcher, "process_identity", side_effect=[(True, 42), (True, 42),
                                                                    (False, None), (False, None)]), \
             patch("psutil.Process") as process:
            process.return_value.exe.return_value = str(self.config.python_exe)
            process.return_value.cmdline.return_value = [str(self.config.python_exe),
                                                            str(self.root / "bridge" / "chat_server.py")]
            self.assertEqual(launcher.stop_owned_bridge(self.config, timeout=0.5),
                             {"stopped": True, "pid": 47231})
        self.assertEqual(server.control_tokens, ["a" * 64])

        self.owned_record(token="")
        server.control_tokens.clear()
        with patch.object(launcher, "process_identity", return_value=(True, 42)):
            with self.assertRaisesRegex(launcher.LauncherError, "no valid control token"):
                launcher.stop_owned_bridge(self.config, timeout=0.5)
        self.assertEqual(server.control_tokens, [])

    def test_stop_rejects_stale_pid_wrong_image_and_wrong_service(self):
        server = self.server("real-ui-1")
        self.owned_record()
        with patch.object(launcher, "process_identity", return_value=(True, 99)):
            with self.assertRaisesRegex(launcher.LauncherError, "Expected one owned bridge record"):
                launcher.stop_owned_bridge(self.config)
        (self.root / "other.py").write_text("fixture only", encoding="utf-8")
        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch("psutil.Process") as process:
            process.return_value.exe.return_value = str(self.config.python_exe)
            process.return_value.cmdline.return_value = [str(self.config.python_exe),
                                                            str(self.root / "other.py")]
            with self.assertRaisesRegex(launcher.LauncherError, "image or script command line"):
                launcher.stop_owned_bridge(self.config)
        self.assertEqual(server.control_tokens, [])
        server.RequestHandlerClass.version = "wrong-version"
        with self.assertRaisesRegex(launcher.LauncherError, "wrong service"):
            launcher.stop_owned_bridge(self.config)
        self.assertEqual(server.control_tokens, [])

    def test_stop_timeout_uses_only_verified_bounded_cleanup(self):
        server = self.server("real-ui-1")
        self.owned_record()
        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch("psutil.Process") as process, \
             patch.object(launcher, "finish_owned_shutdown") as finish:
            process.return_value.exe.return_value = str(self.config.python_exe)
            process.return_value.cmdline.return_value = [str(self.config.python_exe),
                                                            str(self.root / "bridge" / "chat_server.py")]
            self.assertEqual(launcher.stop_owned_bridge(self.config, timeout=0.05),
                             {"stopped": True, "pid": 47231, "forced": True})
            process.return_value.kill.assert_not_called()
            process.return_value.terminate.assert_not_called()
            self.assertEqual(finish.call_count, 1)
            self.assertEqual(finish.call_args.args[1]["pid"], 47231)
        self.assertEqual(server.control_tokens, ["a" * 64])

    def test_unavailable_owned_bridge_is_waited_out_without_second_shutdown(self):
        self.owned_record()
        with patch.object(launcher, "process_identity", side_effect=[(True, 42), (True, 42),
                                                                    (False, None), (False, None)]), \
             patch("psutil.Process") as process, \
             patch.object(launcher, "request_owned_shutdown") as request:
            process.return_value.exe.return_value = str(self.config.python_exe)
            process.return_value.cmdline.return_value = [str(self.config.python_exe),
                                                            str(self.root / "bridge" / "chat_server.py")]
            self.assertEqual(launcher.stop_owned_bridge(self.config, timeout=0.5),
                             {"stopped": True, "pid": 47231})
            request.assert_not_called()

    def test_real_isolated_bridge_finishes_after_port_closes(self):
        process = self.isolated_shutdown_fixture(exit_delay=8)
        launcher.request_owned_shutdown(self.config, "a" * 64)
        deadline = time.monotonic() + 3
        while launcher.health(self.config) != "unavailable" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(launcher.health(self.config), "unavailable")
        self.assertIsNone(process.poll(), "bridge must still be draining after the port closes")
        self.assertEqual(launcher.stop_owned_bridge(self.config, timeout=12),
                         {"stopped": True, "pid": process.pid})
        process.wait(timeout=3)
        self.assertIsNotNone(process.poll())

    def test_real_isolated_bridge_is_bounded_after_grace_timeout(self):
        process = self.isolated_shutdown_fixture(exit_delay=60)
        self.assertEqual(launcher.stop_owned_bridge(self.config, timeout=0.25),
                         {"stopped": True, "pid": process.pid, "forced": True})
        process.wait(timeout=3)
        self.assertIsNotNone(process.poll())

    def test_selected_python_command_uses_path_image(self):
        self.assertEqual(launcher.selected_python_exe("python"),
                         Path(shutil.which("python")).resolve())

    def test_forced_cleanup_interrupts_only_verified_analysis_worker(self):
        from unittest.mock import Mock
        record = {"pid": 47231, "created_filetime": 42}
        parent = Mock()
        child = Mock(pid=12345)
        with patch.object(launcher, "bridge_exited", side_effect=[False, False, True]), \
             patch.object(launcher, "verify_owned_process", return_value=parent), \
             patch.object(launcher, "owned_model_children", return_value=[child]), \
             patch("psutil.wait_procs", return_value=([], [])):
            launcher.finish_owned_shutdown(self.config, record, [], timeout=1)
        child.terminate.assert_called_once()
        parent.terminate.assert_not_called()

    def test_analysis_child_match_requires_direct_child_and_exact_script(self):
        from unittest.mock import Mock
        node = self.root / "node.exe"
        node.write_bytes(b"fixture")
        script = self.root / "bridge" / "analysis_server.ts"
        script.write_text("fixture only", encoding="utf-8")
        expected = Mock()
        expected.exe.return_value = str(node)
        expected.cmdline.return_value = [str(node), "--import", "tsx", str(script), "--provider", "cpu"]
        unrelated = Mock()
        unrelated.exe.return_value = str(self.config.python_exe)
        unrelated.cmdline.return_value = expected.cmdline.return_value
        wrong_script = Mock()
        wrong_script.exe.return_value = str(node)
        wrong_script.cmdline.return_value = [str(node), "--import", "tsx", str(self.root / "other.ts")]
        parent = Mock()
        parent.children.return_value = [expected, unrelated, wrong_script]
        self.assertEqual(launcher.owned_model_children(self.config, parent), [expected])

    def test_bounded_cleanup_terminates_only_verified_bridge_after_worker_drains(self):
        from unittest.mock import Mock
        record = {"pid": 47231, "created_filetime": 42}
        parent = Mock()
        with patch.object(launcher, "bridge_exited", return_value=False), \
             patch.object(launcher, "verify_owned_process", return_value=parent), \
             patch.object(launcher, "owned_model_children", return_value=[]), \
             patch.object(launcher, "wait_for_bridge_exit", side_effect=[False, True]), \
             patch("psutil.wait_procs", return_value=([], [])):
            launcher.finish_owned_shutdown(self.config, record, [], timeout=1)
        parent.terminate.assert_called_once()

    def test_installed_runtime_rejects_external_python_image(self):
        portable = self.root / "runtime" / "python" / "python.exe"
        portable.parent.mkdir(parents=True)
        portable.write_bytes(b"fixture")
        with patch.object(launcher, "process_identity", return_value=(True, 42)), \
             patch("psutil.Process") as process:
            process.return_value.exe.return_value = str(self.config.python_exe)
            process.return_value.cmdline.return_value = [str(self.config.python_exe),
                                                            str(self.root / "bridge" / "chat_server.py")]
            with self.assertRaisesRegex(launcher.LauncherError, "installed runtime"):
                launcher.verify_owned_process(self.config, {"pid": 47231, "created_filetime": 42})

    def test_stop_without_service_is_idempotent_but_unverified_record_aborts(self):
        self.assertEqual(launcher.stop_owned_bridge(self.config),
                         {"stopped": False, "alreadyStopped": True})
        self.owned_record()
        with patch.object(launcher, "process_identity", return_value=(True, 42)):
            with self.assertRaisesRegex(launcher.LauncherError, "Cannot verify bridge process ownership"):
                launcher.stop_owned_bridge(self.config, timeout=0.05)

    def test_stop_without_service_reclaims_dead_record_and_workers(self):
        self.owned_record()
        with patch.object(launcher, "process_identity", return_value=(False, None)), \
             patch.object(launcher, "abort_owned_launch", return_value=[]) as reclaim:
            self.assertEqual(launcher.stop_owned_bridge(self.config),
                             {"stopped": True, "alreadyStopped": True})
        reclaim.assert_called_once()

    def test_stop_parent_exit_race_uses_bounded_abort_cleanup(self):
        self.server("real-ui-1")
        self.owned_record()
        with patch.object(launcher, "process_identity", side_effect=[(True, 42), (False, None)]), \
             patch.object(launcher, "verify_owned_process",
                          side_effect=launcher.LauncherError("process exited")), \
             patch.object(launcher, "abort_owned_launch", return_value=[]) as reclaim:
            self.assertEqual(launcher.stop_owned_bridge(self.config, timeout=0.5),
                             {"stopped": True, "pid": 47231})
        reclaim.assert_called_once()

    def test_stop_json_command_reports_outcome(self):
        with patch.object(launcher, "PROJECT_ROOT", self.root), \
             patch.dict("os.environ", {"CHATUI_PORT": str(self.config.port)}), \
             patch.object(launcher, "stop_owned_bridge", return_value={"stopped": False, "alreadyStopped": True}), \
             patch.object(launcher, "open_client", side_effect=AssertionError("GUI opened")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launcher.main(["--stop-owned-bridge", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"stopped": False, "alreadyStopped": True})

    def test_mutex_is_shared_with_another_process(self):
        marker = self.root / "child acquired.txt"
        child_code = (
            "import importlib.util, pathlib, sys; "
            "spec=importlib.util.spec_from_file_location('launcher_child', sys.argv[1]); "
            "module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; "
            "spec.loader.exec_module(module); "
            "config=module.Config(pathlib.Path(sys.argv[2]), pathlib.Path(sys.executable), int(sys.argv[3])); "
            "lock=module.launch_mutex(config); lock.__enter__(); "
            "pathlib.Path(sys.argv[4]).write_text('acquired'); lock.__exit__(None,None,None)"
        )
        with launcher.launch_mutex(self.config):
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", child_code, str(SCRIPT), str(self.root),
                 str(self.config.port), str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(0.35)
            self.assertIsNone(child.poll())
            self.assertFalse(marker.exists())
        output, errors = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, (output, errors))
        self.assertEqual(marker.read_text(), "acquired")

    def test_dedicated_shell_required_without_browser_fallback(self):
        with patch.object(launcher.subprocess, "Popen") as process:
            with self.assertRaisesRegex(launcher.LauncherError, "Electron shell is unavailable"):
                launcher.open_client(self.config.url, self.root)
            process.assert_not_called()
        electron = self.root / "node_modules" / "electron" / "dist" / "electron.exe"
        electron.parent.mkdir(parents=True)
        electron.write_bytes(b"synthetic")
        shell = self.root / "scripts" / "real-client-shell.cjs"
        shell.parent.mkdir(parents=True)
        shell.write_text("synthetic", encoding="utf-8")
        with patch.object(launcher.subprocess, "Popen") as process:
            launcher.open_client(self.config.url, self.root)
            process.assert_called_once()
            args, kwargs = process.call_args
            self.assertEqual(args, ([str(electron), str(shell), "--client-url", self.config.url],))
            self.assertEqual(kwargs["cwd"], str(self.root))
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(kwargs["env"]["WECHATVIBE_INSTANCE_ID"], self.config.instance_id)
        with patch.object(launcher.subprocess, "Popen", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(launcher.LauncherError, "Could not open dedicated Electron shell"):
                launcher.open_client(self.config.url, self.root)

    def test_status_is_read_only_and_no_open_skips_browser(self):
        self.server("real-ui-1")
        with patch.object(launcher, "PROJECT_ROOT", self.root), patch.dict("os.environ", {"CHATUI_PORT": str(self.config.port)}), patch.object(launcher, "open_client", side_effect=AssertionError("GUI opened")):
            self.assertEqual(launcher.main(["--status"]), 0)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launcher.main(["--no-open", "--json"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result, {"version": launcher.VERSION, "url": self.config.url,
                                      "instanceId": self.config.instance_id, "created": False})
        self.assertFalse(self.config.runtime_dir.exists())

    def test_current_clear_marker_blocks_recovery_until_explicit_launch(self):
        marker = self.config.no_auto_recovery_marker
        marker.parent.mkdir(parents=True)
        marker.write_text('{"reason":"account-cleared"}', encoding='utf-8')
        with patch.object(launcher, 'health', side_effect=AssertionError('health probed')):
            with self.assertRaisesRegex(launcher.LauncherError, 'Automatic recovery is disabled'):
                launcher.ensure_service(self.config, starter=lambda *_: self.fail('started'), recovery=True)
        self.assertTrue(marker.exists())
        self.server('real-ui-1')
        self.assertEqual(launcher.ensure_service(self.config, starter=lambda *_: self.fail('started')),
                         (False, None))
        self.assertFalse(marker.exists())
        self.assertEqual(launcher.ensure_service(self.config, starter=lambda *_: self.fail('started'),
                                                 recovery=True), (False, None))

    def test_failed_explicit_launch_preserves_no_recovery_marker(self):
        marker = self.config.no_auto_recovery_marker
        marker.parent.mkdir(parents=True)
        marker.write_text('{"reason":"account-cleared"}', encoding='utf-8')
        self.server('wrong-version')
        with self.assertRaisesRegex(launcher.LauncherError, 'occupied'):
            launcher.ensure_service(self.config, starter=lambda *_: self.fail('started'))
        self.assertTrue(marker.exists())

    def test_recovery_command_does_not_open_or_start_when_marker_exists(self):
        marker = self.config.no_auto_recovery_marker
        marker.parent.mkdir(parents=True)
        marker.write_text('{"reason":"account-cleared"}', encoding='utf-8')
        with patch.object(launcher, 'PROJECT_ROOT', self.root), patch.object(launcher, 'PYTHON_EXE', Path(sys.executable)), \
             patch.dict('os.environ', {'CHATUI_PORT': str(self.config.port)}), \
             patch.object(launcher, 'open_client', side_effect=AssertionError('GUI opened')):
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(launcher.main(['--no-open', '--recovery']), 1)
        self.assertIn('Automatic recovery is disabled', errors.getvalue())
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()

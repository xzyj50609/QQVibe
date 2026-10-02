"""Production connector/coordinator against temporary synthetic loopback services."""
import copy
import json
import math
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probes"))
import qce_probe
import qce_protocol
from fake_qce_server import RunningFakeQCE, DEFAULT_TOKEN, BASE_EPOCH_S
import test_qq_accounts as accounts_fixture
from qq_connector import QQConnector, ConnectorError, SyncPolicy, SyncCancelled, failure_code
from qq_sync import QQSync
from qq_sync_config import SyncConnectionStore
from qq_message_store import QQMessageStore
from test_qq_message_store import CONV

BASE = BASE_EPOCH_S * 1000


def rows(server):
    for index, row in enumerate(server.server.dataset):
        row["text"] = "合成消息" + str(index)
        row["senderUid"] = "u_synthetic_self" if row["senderUin"] == "10001" else "u_peer_synth01"


class ConnectorTests(unittest.TestCase):
    def server(self, **values):
        server = RunningFakeQCE({"datasetSize": 10, "stepSeconds": 1, "sameSecondPairs": 0, **values})
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        rows(server)
        return server

    def scan(self, server, **values):
        policy = values.pop("policy", SyncPolicy(page_size=5, batch_size=5, max_pages=5))
        return QQConnector(server.base, DEFAULT_TOKEN, policy=policy).scan("10001", "u_peer_synth01", BASE, BASE + 20000, **values)

    def test_probe_and_production_share_the_same_pagination_implementation(self):
        self.assertIs(qce_probe.bounded_fetch, qce_protocol.bounded_fetch)

    def test_actual_http_scan_preserves_ids_body_direction_and_counts_all_requests(self):
        server = self.server()
        result = self.scan(server)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["records"]), 10)
        self.assertEqual({item["direction"] for item in result["records"]}, {"self", "peer"})
        self.assertTrue(all(item["text"].startswith("合成消息") for item in result["records"]))
        self.assertEqual(result["requests"], len(server.requests))
        self.assertEqual(result["requests"], result["fetchRequests"] + 4)
        self.assertTrue(all(item["conversation_key"] == CONV for item in result["records"]))

    def test_owner_mismatch_stops_before_reading_messages(self):
        server = self.server(selfUin="10002")
        with self.assertRaisesRegex(ConnectorError, "account-changed"):
            self.scan(server)
        self.assertFalse(any(request["method"] == "POST" for request in server.requests))

    def test_identity_change_after_scanning_discards_all_staged_rows(self):
        server = self.server(selfUinAfter="10002")
        with self.assertRaisesRegex(ConnectorError, "account-changed"):
            self.scan(server)
        self.assertTrue(any(request["method"] == "POST" for request in server.requests))

    def test_missing_metadata_repeated_and_bad_pages_are_not_complete(self):
        for mode in ("missing-metadata", "repeat-same", "empty-with-more", "malformed-items"):
            with self.subTest(mode=mode):
                server = self.server(fetch=mode)
                result = self.scan(server)
                self.assertEqual(result["status"], "partial")
                self.assertTrue(result["reason"])

    def test_auth_business_bad_json_and_redirect_fail_with_fixed_codes(self):
        for scenario, expected in (({"fetch": "auth-401"}, "auth-required"),
                                   ({"fetch": "business-leaky-message"}, "source-rejected"),
                                   ({"fetch": "bad-json"}, "protocol-invalid"),
                                   ({"redirect": True}, "protocol-invalid")):
            with self.subTest(scenario=scenario):
                server = self.server(**scenario)
                with self.assertRaises(ConnectorError) as caught:
                    self.scan(server)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn(DEFAULT_TOKEN, str(caught.exception))

    def test_unknown_endpoints_group_fetch_and_unbounded_requests_are_rejected_before_io(self):
        server = self.server()
        client = QQConnector(server.base, DEFAULT_TOKEN).client()
        cases = [("POST", "/api/messages/export", {}), ("GET", "/api/friends", None),
                 ("GET", "/api/system/info?token=private", None),
                 ("POST", "/api/messages/fetch", {"peer": {"chatType": 2, "peerUid": "u_peer"}})]
        for method, path, body in cases:
            with self.subTest(path=path), self.assertRaises(ConnectorError):
                client.request("get", method, path, "fixed-label", body=body)
        self.assertEqual(server.requests, [])

    def test_cancel_after_response_and_wrong_time_or_peer_scope_never_publish(self):
        server = self.server()
        for row in server.server.dataset:
            row["peerUid"] = "u_wrong_peer"
        result = self.scan(server)
        self.assertEqual((result["status"], result["reason"], result["records"]), ("partial", "normalization-rejected", []))
        def cancel():
            if len(server.requests) > result["requests"]:
                raise SyncCancelled()
        with self.assertRaises(SyncCancelled):
            self.scan(server, check=cancel)

    def test_page_budget_is_partial_and_page_size_stays_fixed(self):
        server = self.server(datasetSize=100)
        result = self.scan(server, policy=SyncPolicy(page_size=5, batch_size=5, max_pages=2, max_messages=7))
        self.assertEqual(result["status"], "partial")
        bodies = [item["body"] for item in server.requests if item["method"] == "POST"]
        self.assertTrue(all(item["limit"] == 5 for item in bodies))
        self.assertLessEqual(len(result["records"]), 7)

    def test_zero_count_empty_page_with_more_is_partial(self):
        class Client:
            budget = SyncPolicy().budget()
            def request(self, *_args, **_kwargs):
                return 200, {"success": True, "data": {"messages": [], "currentPage": 1,
                    "totalPages": 0, "totalCount": 0, "hasNext": True}}, 0
        result = qce_protocol.bounded_fetch(Client(), SimpleNamespace(peer_uid="u_peer", limit=50, batch_size=200), (0, 1000))
        self.assertEqual(result["status"], "partial")

    def test_every_observed_version_of_a_duplicate_native_id_reaches_production(self):
        class Client:
            budget = SyncPolicy(page_size=2).budget()
            def request(self, *_args, **_kwargs):
                return 200, {"success": True, "data": {"messages": [{"msgId": "1", "text": "first"},
                    {"msgId": "1", "text": "changed"}], "currentPage": 1, "totalPages": 1,
                    "totalCount": 2, "hasNext": False}}, 0
        unique, all_versions = [], []
        result = qce_protocol.bounded_fetch(Client(), SimpleNamespace(peer_uid="u_peer", limit=2, batch_size=200),
            (0, 1000), on_rows=lambda values, _: unique.extend(values), on_page=lambda values, _: all_versions.extend(values))
        self.assertEqual(result["status"], "complete")
        self.assertEqual((len(unique), len(all_versions)), (1, 2))

    def test_policy_nonfinite_boolean_and_loopback_escape_are_rejected(self):
        for value in (0, True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(Exception):
                SyncPolicy(max_seconds=value)
        for base in ("https://untrusted.example", "http://user:private@127.0.0.1", "http://127.0.0.1?token=private"):
            with self.subTest(base=base), self.assertRaises(Exception):
                QQConnector(base, DEFAULT_TOKEN)

    def test_qce630_lookup_uses_uid_and_rejects_conflicting_identity_fields(self):
        from unittest.mock import Mock
        connector = QQConnector("http://127.0.0.1:12345", DEFAULT_TOKEN)
        for data, accepted in [
            ({"found": True, "isFriend": True, "uin": "10002", "uid": "u_peer_synth01"}, True),
            ({"found": True, "peerUid": "u_peer_synth01"}, True),
            ({"found": True, "uin": "10003", "uid": "u_peer_synth01"}, False),
            ({"found": True, "uid": "u_peer_synth01", "peerUid": "u_another_peer"}, False),
        ]:
            client = Mock()
            client.request.return_value = (200, {"success": True, "data": data}, 0)
            connector.client = Mock(return_value=client)
            connector.identity = Mock(return_value={"ownerUin": "10001"})
            if accepted:
                self.assertEqual(connector.resolve_peer("10001", "10002")["peerUid"], "u_peer_synth01")
            else:
                with self.assertRaisesRegex(ConnectorError, "peer-identity-invalid"):
                    connector.resolve_peer("10001", "10002")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.fixture = accounts_fixture.QQAccountTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.api, self.source, self.backend = self.fixture.api, self.fixture.source, self.fixture.backend
        self.account, _ = self.fixture.bind("10001")
        self.backend.selection_store.set_selected(self.account, CONV, True)
        self.server = RunningFakeQCE({"datasetSize": 10, "stepSeconds": 1, "sameSecondPairs": 0})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        rows(self.server)
        self.config = SyncConnectionStore(self.fixture.root / "QQVibeData/real-client-runtime/qq-connection.json",
            protect=lambda value: ("ENCRYPTED:" + value).encode(), unprotect=lambda value: value.decode()[10:])
        self.commits = []
        self.sync = QQSync(self.api, live_validated=True, config_store=self.config,
            policy=SyncPolicy(page_size=5, batch_size=5, max_pages=3), autostart=False,
            on_commit=lambda *values: self.commits.append(values), wall_ms=lambda: BASE + 20000)
        self.api.sync_manager = self.sync
        self.backend.qq_sync = self.sync
        self.addCleanup(self.sync.close)
        self.sync.configure(self.server.base, "10001", DEFAULT_TOKEN)
        self.sync.connect()

    def checkpoint(self):
        with self.source.read_library(self.account) as (_, library):
            return library.checkpoint(self.account, CONV)

    def test_success_time_never_leaks_to_a_new_connection_owner(self):
        self.assertEqual(self.sync.run_once(CONV)["state"], "complete")
        self.assertEqual(self.sync.public()["lastSuccessAtMs"], BASE + 20000)
        self.sync.configure(self.server.base, "20002", DEFAULT_TOKEN)
        self.assertIsNone(self.sync.public()["lastSuccessAtMs"])
        self.assertEqual(self.sync.public()["ownerUin"], "20002")

    def test_invalid_connection_edit_preserves_previous_success_time(self):
        self.sync.run_once(CONV)
        with self.assertRaises(ConnectorError):
            self.sync.configure("https://remote.invalid", "20002", DEFAULT_TOKEN)
        self.assertEqual(self.sync.public()["lastSuccessAtMs"], BASE + 20000)
        self.assertFalse(self.sync.suspended)
        self.assertTrue(self.sync.public()["enabled"])

    def test_saving_the_same_endpoint_and_owner_keeps_the_success_time(self):
        self.sync.run_once(CONV)
        self.sync.configure(self.server.base, "0010001")
        self.assertEqual(self.sync.public()["lastSuccessAtMs"], BASE + 20000)

    def test_secret_protection_failure_is_paused_and_keeps_the_previous_profile(self):
        self.sync.run_once(CONV)
        original = self.config.path.read_bytes()
        with patch.object(self.config, "protect", side_effect=OSError("synthetic encryption failure")):
            with self.assertRaises(ConnectorError):
                self.sync.configure(self.server.base, "20002", "SYNTHETIC_REPLACEMENT")
        self.assertEqual(self.config.path.read_bytes(), original)
        self.assertEqual(self.sync.public()["state"], "paused")
        self.assertEqual(self.sync.public()["reason"], "credential-storage-unavailable")
        self.assertEqual(self.sync.public()["lastSuccessAtMs"], BASE + 20000)

    def test_forward_scan_commits_messages_and_checkpoint_then_schedules_analysis(self):
        result = self.sync.run_once(CONV)
        self.assertEqual((result["state"], result["inserted"]), ("complete", 10))
        checkpoint = self.checkpoint()
        self.assertEqual(checkpoint["scanned_through_ms"], BASE + 20000)
        self.assertEqual(checkpoint["state"], "COMMITTED")
        self.assertEqual(self.commits, [(self.account, CONV)])
        again = self.sync.run_once(CONV, now_ms=BASE + 21000)
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(len(self.commits), 1)

    def test_new_message_continues_from_persistent_overlap_and_source_ids(self):
        self.sync.run_once(CONV)
        extra = copy.deepcopy(self.server.server.dataset[-1])
        extra.update(msgId="9007199254741200", msgTime=str(BASE_EPOCH_S + 21), text="新来的合成消息")
        self.server.server.dataset.append(extra)
        result = self.sync.run_once(CONV, now_ms=BASE + 22000)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.checkpoint()["window_start_ms"], BASE + 18000)
        self.assertTrue(any(item["text"] == "新来的合成消息" for item in self.source.messages(CONV, 100)))

    def test_partial_replays_the_same_window_with_more_budget_then_stops(self):
        self.server.server.scenario["fetch"] = "repeat-same"
        windows = []
        for attempt in range(3):
            result = self.sync.run_once(CONV, now_ms=BASE + 20000 + attempt * 1000)
            self.assertEqual(result["state"], "partial")
            checkpoint = self.checkpoint()
            windows.append((checkpoint["window_start_ms"], checkpoint["window_end_ms"]))
            self.assertEqual(checkpoint["scanned_through_ms"], 0)
        count = len(self.server.requests)
        result = self.sync.run_once(CONV)
        self.assertEqual(result["reason"], "retry-limit")
        self.assertEqual(len(self.server.requests), count)
        self.assertEqual(len(set(windows)), 1)
        self.assertTrue(math.isinf(self.sync.due[(self.account, CONV)]))

    def test_auth_failure_stops_and_leaves_local_messages_readable(self):
        self.server.server.expected_token = "A_DIFFERENT_SYNTHETIC_TOKEN"
        result = self.sync.run_once(CONV)
        self.assertEqual(result["reason"], "auth-required")
        self.assertFalse(self.config.read()["enabled"])
        self.assertEqual(len(self.source.messages(CONV, 100)), 1)
        count = len(self.server.requests)
        with self.assertRaisesRegex(ConnectorError, "sync-disabled"):
            self.sync.run_once(CONV)
        self.assertEqual(len(self.server.requests), count)

    def test_unselected_conversation_is_never_fetched(self):
        self.backend.set_conversation_selected(self.account, CONV, False)
        count = len(self.server.requests)
        with self.assertRaisesRegex(ConnectorError, "conversation-not-selected"):
            self.sync.run_once(CONV)
        self.assertEqual(len(self.server.requests), count)

    def test_late_scan_after_a_to_b_to_a_is_discarded_with_no_checkpoint(self):
        original = self.sync.connector.scan
        def switch(*values, **options):
            result = original(*values, **options)
            self.source.attach("10003")
            self.source.attach("10001")
            return result
        with patch.object(self.sync.connector, "scan", switch):
            result = self.sync.run_once(CONV)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(self.checkpoint())
        self.assertEqual(len(self.source.messages(CONV, 100)), 1)

    def test_final_commit_guard_rejects_deselection_and_rolls_back_every_row(self):
        original = QQMessageStore.ingest
        def deselect(library, *values, **options):
            if options.get("checkpoint") is not None:
                self.backend.selection_store.set_selected(self.account, CONV, False)
            return original(library, *values, **options)
        with patch.object(QQMessageStore, "ingest", deselect):
            result = self.sync.run_once(CONV)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(self.checkpoint())
        self.assertEqual(len(self.source.messages(CONV, 100)), 1)

    def test_clear_account_persists_disabled_connection_before_deleting_files(self):
        self.fixture.bind("10003")
        identifier = self.api.store.register(self.account, self.api.store._workdir(self.account), owner_uin="10001")
        self.api.delete(identifier)
        self.assertFalse(self.config.read()["enabled"])
        self.assertEqual([item["displayId"] for item in self.api.list()["accounts"]], ["10003"])

    def test_restart_replays_partial_window_from_page_one(self):
        self.server.server.scenario["fetch"] = "repeat-same"
        self.sync.run_once(CONV)
        previous = self.checkpoint()
        self.sync.close()
        restarted = QQSync(self.api, live_validated=True, config_store=self.config,
            policy=self.sync.policy, autostart=False, wall_ms=lambda: BASE + 80000)
        self.addCleanup(restarted.close)
        self.backend.qq_sync = restarted
        self.api.sync_manager = restarted
        self.server.server.scenario["fetch"] = "dataset"
        before = len(self.server.requests)
        result = restarted.run_once(CONV)
        self.assertEqual(result["state"], "complete")
        fetches = [request for request in self.server.requests[before:] if request["method"] == "POST"]
        self.assertEqual(fetches[0]["body"]["page"], 1)
        self.assertEqual(self.checkpoint()["window_start_ms"], previous["window_start_ms"])
        self.assertEqual(self.checkpoint()["scanned_through_ms"], previous["window_end_ms"])

    def test_no_live_validation_refuses_connect_and_scan_without_http(self):
        closed = QQSync(self.api, config_store=self.config, autostart=False)
        self.addCleanup(closed.close)
        before = len(self.server.requests)
        for action in (closed.connect, lambda: closed.run_once(CONV)):
            with self.assertRaisesRegex(ConnectorError, "connector-awaiting-validation"):
                action()
        self.assertEqual(len(self.server.requests), before)

    def test_minute_request_budget_is_global_and_recovers_after_the_window_expires(self):
        clock = [self.sync.monotonic()]
        self.sync.monotonic = lambda: clock[0]
        while self.sync.requests_total < self.sync.policy.requests_per_minute:
            self.sync._charge_request()
        with self.assertRaisesRegex(ConnectorError, "request-budget-exhausted"):
            self.sync._charge_request()
        clock[0] += 61
        self.sync._charge_request()
        self.assertEqual(len(self.sync.request_times), 1)

    def test_metadata_lookup_adds_exactly_the_chosen_contact_without_reading_messages(self):
        before = len(self.server.requests)
        result = self.sync.add_contact("20001", "选择的合成好友")
        self.assertEqual((result["selected"], result["user"]), (True, "u:u_synthetic_peer"))
        self.assertFalse(any(request["method"] == "POST" for request in self.server.requests[before:]))
        self.assertEqual(self.source.contact(result["user"])["name"], "选择的合成好友")
        self.assertIn(result["user"], self.backend.selection_store.get(self.account)["selectedSessions"])

    def test_core_first_add_reads_bounded_history_once_and_keeps_forward_cursor_separate(self):
        from unittest.mock import Mock
        from test_qq_message_store import record
        self.sync.initial_read = True
        value = record("initial-1", time_ms=BASE + 1000)
        value.update(conversation_key="u:u_synthetic_peer", sender_uid="u_synthetic_peer")
        raw = json.loads(value["raw"])
        raw["peerUid"] = "u_synthetic_peer"
        value["raw"] = json.dumps(raw)
        initial = Mock()
        initial.scan.return_value = {"records": [value], "status": "partial", "reason": "budget-exhausted",
            "version": "6.3.0", "counts": {"rowsRejected": 0}}
        self.sync.connector_factory = Mock(return_value=initial)
        result = self.sync.add_contact("20001", "合成历史初读")
        policy = self.sync.connector_factory.call_args.kwargs["policy"]
        self.assertEqual((policy.max_messages, policy.max_pages), (200, 4))
        arguments = initial.scan.call_args.args
        self.assertEqual(arguments[3] - arguments[2], 90 * 86400000)
        with self.source.read_library(self.account) as (_, library):
            self.assertEqual(library.counts(self.account, result["user"])[0], 1)
            self.assertIsNone(library.checkpoint(self.account, result["user"]))
            self.assertFalse(library.coverage(self.account, result["user"])["covered"])
        self.sync.add_contact("20001", "合成历史初读")
        self.sync.connector_factory.assert_called_once()

    def test_source_reads_remain_available_and_account_activation_drains_a_blocked_scan(self):
        self.fixture.bind("10003")
        from account_store import account_id
        self.api.activate(account_id(self.account))
        entered = threading.Event()
        old_epoch = self.sync.epoch
        outputs = []
        def blocked(*values, **options):
            entered.set()
            deadline = time.monotonic() + 4
            while self.sync.epoch == old_epoch and time.monotonic() < deadline:
                threading.Event().wait(.01)
            options["check"]()
            raise AssertionError("stale scan was not cancelled")
        with patch.object(self.sync.connector, "scan", blocked):
            thread = threading.Thread(target=lambda: outputs.append(self.sync.run_once(CONV)), daemon=True)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(len(self.source.messages(CONV, 100)), 1)
            target = next(item for item in self.api.list()["accounts"] if item["displayId"] == "10003")
            self.api.activate(target["accountId"])
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outputs[0]["state"], "cancelled")
        self.assertEqual(self.source.identity()[0], target["account"] if "account" in target else __import__('qq_identity').account_key("10003"))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix="qq-sync-settings-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime/connection.json"
        self.store = SyncConnectionStore(self.path, protect=lambda value: ("ENCRYPTED:" + value).encode(),
                                         unprotect=lambda value: value.decode()[10:])

    def test_empty_config_is_readonly_and_saved_bearer_is_not_plaintext_or_public(self):
        self.assertFalse(self.store.public()["tokenConfigured"])
        self.assertFalse(self.path.parent.exists())
        public = self.store.configure("http://127.0.0.1:12345", "0010001", DEFAULT_TOKEN)
        self.assertEqual(public["ownerUin"], "10001")
        self.assertNotIn(DEFAULT_TOKEN, self.path.read_text(encoding="utf-8"))
        self.assertNotIn(DEFAULT_TOKEN, json.dumps(public))
        self.assertEqual(self.store.token(), DEFAULT_TOKEN)

    def test_reusing_secret_requires_the_same_origin_and_owner(self):
        self.store.configure("http://127.0.0.1:12345", "10001", DEFAULT_TOKEN)
        self.store.configure("http://127.0.0.1:12345", "10001")
        self.assertTrue(self.store.public()["tokenConfigured"])
        self.store.configure("http://127.0.0.1:12345", "10003")
        self.assertFalse(self.store.public()["tokenConfigured"])

    def test_clear_disables_connection_and_corrupt_settings_never_fall_back_to_plaintext(self):
        self.store.configure("http://127.0.0.1:12345", "10001", DEFAULT_TOKEN)
        self.store.set_enabled(True)
        self.store.clear_token()
        self.assertEqual((self.store.public()["enabled"], self.store.public()["tokenConfigured"]), (False, False))
        self.path.write_text("malformed", encoding="utf-8")
        with self.assertRaisesRegex(ConnectorError, "configuration-unavailable"):
            self.store.read()

    def test_real_windows_dpapi_roundtrip_uses_only_a_generated_synthetic_token(self):
        actual = SyncConnectionStore(self.path)
        actual.configure("http://127.0.0.1:12345", "10001", DEFAULT_TOKEN)
        self.assertNotIn(DEFAULT_TOKEN, self.path.read_text(encoding="utf-8"))
        self.assertEqual(actual.token(), DEFAULT_TOKEN)


class SlowTransportTests(unittest.TestCase):
    def check_slow(self, headers):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                payload = b'{"success":true,"data":{}}'
                head = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\nConnection: close\r\n\r\n'
                try:
                    if not headers:
                        self.connection.sendall(head)
                    for byte in head + payload if headers else payload:
                        self.connection.sendall(bytes([byte]))
                        time.sleep(.02)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                self.close_connection = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        policy = SyncPolicy(max_seconds=.09, request_timeout=2)
        client = QQConnector(f"http://127.0.0.1:{server.server_port}", DEFAULT_TOKEN, policy=policy).client()
        started = time.monotonic()
        with self.assertRaises(qce_protocol.BudgetExhausted):
            client.request("get", "GET", "/api/system/info", "fixed-label")
        self.assertLess(time.monotonic() - started, .7)

    def test_slow_drip_headers_cannot_extend_the_absolute_deadline(self):
        self.check_slow(True)

    def test_slow_drip_body_cannot_extend_the_absolute_deadline(self):
        self.check_slow(False)


if __name__ == "__main__":
    unittest.main()

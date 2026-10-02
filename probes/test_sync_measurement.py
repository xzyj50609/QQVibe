"""Review 09 M01-M06 regressions. Synthetic loopback server and temporary state only."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sync_measurement as tool
import qce_probe
from fake_qce_server import BASE_EPOCH_S, DEFAULT_TOKEN, RunningFakeQCE

PROBES = Path(__file__).resolve().parent
REPO_ROOT = PROBES.parent
TOKEN = DEFAULT_TOKEN
START = BASE_EPOCH_S * 1000
SALT = b"x" * 32
OWNER = "10001"
PEER = "u_synthetic_peer"
RUN = "a" * 32
SCOPE = tool.measurement_scope(SALT, OWNER, PEER)


def run_tool(state, argv, base="http://127.0.0.1:9"):
    env = dict(os.environ, QCE_TOKEN=TOKEN)
    env.pop("QCE_BASE", None)
    result = subprocess.run([sys.executable, str(PROBES / "sync_measurement.py"),
                             "--base", base, "--state-dir", str(state), *argv],
                            capture_output=True, text=True, env=env, timeout=30)
    payloads = [json.loads(line[len("MEASURE_JSON="):]) for line in result.stdout.splitlines()
                if line.startswith("MEASURE_JSON=")]
    return result.returncode, payloads[-1] if payloads else None, result.stdout, result.stderr


def snapshot_args(label, *extra):
    return ["cp06-snapshot", "--peer-uid", PEER, "--window-start-ms", str(START),
            "--window-end-ms", str(START + 12 * 60000), "--label", label,
            "--limit", "5", "--batch-size", "50", "--max-total-requests", "24",
            "--max-fetch-requests", "20", *extra]


def scope(window=True, *, owner=OWNER, peer=PEER, salt=SALT):
    return {**tool.measurement_scope(salt, owner, peer),
            **({"windowStartMs": START, "windowEndMs": START + 600000} if window else {})}


def snapshot(ids, *, at=1000, owner=OWNER, peer=PEER, salt=SALT):
    members = sorted(tool.digest_id(salt, value, account=owner, peer=peer) for value in ids)
    return {"protocol": "CP06", "schema": 2, "scope": scope(owner=owner, peer=peer, salt=salt),
            "takenAtMs": at, "terminalStatus": "complete", "rowCount": len(members),
            "members": members, "setIdDigest": tool.set_digest(members), "attributeShapes": {},
            "clientVersion": {"qceVersion": "6.3.0-fake", "napcatVersion": None}}


def marker(index=0, *, at=1000, kind="sent", armed=True):
    value = {"actionId": f"{index:032x}", "runId": RUN, "scopeDigest": SCOPE["conversationDigest"],
             "kind": kind, "markedAtMs": at + 100}
    if armed:
        value["armedAtMs"] = at
    return value


def poll(index=0, *, at=2000, direction="self", status="complete", run_id=RUN):
    return {"kind": "poll", "runId": run_id, "scopeDigest": SCOPE["conversationDigest"],
            "atMs": at, "status": status,
            "events": [{"eventId": tool.digest_id(SALT, f"event-{index}", account=OWNER, peer=PEER),
                        "direction": direction, "observedAtMs": at}]}


class IdentityAndBudgetTests(unittest.TestCase):
    def test_real_nested_self_info_and_actual_version_values(self):
        with RunningFakeQCE({"fetch": "dataset"}) as server:
            self.assertEqual(tool.resolve_self_uin(server.base, TOKEN), OWNER)
            versions = tool._version_of(server.base, TOKEN)
            self.assertEqual(versions["qceVersion"], "6.3.0-fake")
            self.assertIsNone(versions["napcatVersion"], "unknown NapCat must not be fabricated")
            self.assertEqual(len(server.requests), 3, "identity once plus both version endpoints")

    def test_missing_identity_does_not_fall_back_to_a_fabricated_field(self):
        with RunningFakeQCE({"fetch": "dataset", "selfUin": None}) as server:
            with self.assertRaises(qce_probe.ProbeFailure):
                tool.resolve_self_uin(server.base, TOKEN)

    def test_state_directory_inside_any_git_worktree_is_refused(self):
        inside = REPO_ROOT / "QQVibeMeasure"
        code, payload, out, err = run_tool(inside, snapshot_args("before"))
        self.assertEqual(code, 2)
        self.assertIn("inside the repository", err)
        self.assertFalse(inside.exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            code, _, out, err = run_tool(root / "state", snapshot_args("before"))
            self.assertEqual(code, 2)
            self.assertFalse((root / "state").exists())

    def test_invalid_arguments_and_base_never_send_requests_or_echo_credentials(self):
        variants = [["--max-messages", "0"], ["--request-timeout", "nan"],
                    ["--max-seconds", "inf"], ["--max-pages", "-1"],
                    ["--window-end-ms", "1"], ["--max-fetch-requests", "25"],
                    ["--label", "../bad"], ["--token", TOKEN]]
        with RunningFakeQCE({"fetch": "dataset"}) as server, tempfile.TemporaryDirectory() as state:
            for extra in variants:
                with self.subTest(extra=extra[:1]):
                    code, _, out, err = run_tool(state, snapshot_args("x", *extra), server.base)
                    self.assertEqual(code, 2)
                    self.assertNotIn(TOKEN, out + err)
                    self.assertNotIn("Traceback", out + err)
            code, _, out, err = run_tool(state, snapshot_args("x"), f"http://{TOKEN}@127.0.0.1:9")
            self.assertEqual(code, 2)
            self.assertNotIn(TOKEN, out + err)
            self.assertEqual(server.requests, [])

    def test_salt_is_stable_and_event_digest_is_domain_and_scope_separated(self):
        with tempfile.TemporaryDirectory() as state:
            salt = tool.load_or_create_salt(Path(state))
            self.assertEqual(len(salt), 32)
            self.assertEqual(tool.load_or_create_salt(Path(state)), salt)
        values = {tool.digest_id(SALT, "same", account=OWNER, peer=PEER),
                  tool.digest_id(SALT, "same", account="10002", peer=PEER),
                  tool.digest_id(SALT, "same", account=OWNER, peer="u_other_peer"),
                  tool.digest_id(SALT, "same", account=OWNER, peer=PEER, native_kind="other"),
                  tool.digest_id(b"y" * 32, "same", account=OWNER, peer=PEER)}
        self.assertEqual(len(values), 5)
        self.assertTrue(all(len(value) == 64 for value in values))
        self.assertEqual(tool.set_digest(["a", "b"]), tool.set_digest(["b", "a", "a"]))


class SharedScanTests(unittest.TestCase):
    def args(self, **changes):
        values = dict(peer_uid=PEER, batch_size=3, limit=3, max_pages=20, max_messages=5,
                      max_fetch_requests=20, max_total_requests=24, request_timeout=3,
                      max_seconds=10, max_response_bytes=1024 * 1024)
        values.update(changes)
        return tool.make_probe_args(**values)

    def test_non_multiple_message_budget_never_changes_offset_mid_scan(self):
        with RunningFakeQCE({"fetch": "dataset", "datasetSize": 5, "sameSecondPairs": 0}) as server:
            with patch.object(qce_probe, "bounded_fetch", wraps=qce_probe.bounded_fetch) as shared:
                status, rows = tool.fetch_with_rows(server.base, TOKEN, SALT, OWNER, self.args(),
                                                    (START, START + 5 * 60000))
            self.assertEqual(shared.call_count, 1)
            calls = [request["body"] for request in server.requests if request["method"] == "POST"]
            self.assertEqual([(call["page"], call["limit"]) for call in calls], [(1, 3)])
            self.assertEqual(len(rows), 3)
            self.assertEqual(status["status"], "partial")
            self.assertIn("fixed next page", status["partialReason"])

    def test_duplicate_pages_are_partial_in_both_probe_and_measurement(self):
        with RunningFakeQCE({"fetch": "repeat-same", "sameSecondPairs": 0}) as server:
            status, rows = tool.fetch_with_rows(server.base, TOKEN, SALT, OWNER,
                                                self.args(max_messages=30), (START, START + 12 * 60000))
            self.assertEqual(status["status"], "partial")
            self.assertIn("duplicate", status["partialReason"])
            self.assertEqual(len(rows), 3)

    def test_oversized_response_cannot_exceed_the_message_budget(self):
        with RunningFakeQCE({"fetch": "over-limit", "sameSecondPairs": 0}) as server:
            status, rows = tool.fetch_with_rows(server.base, TOKEN, SALT, OWNER,
                                                self.args(limit=1, max_messages=1), (START, START + 12 * 60000))
            self.assertEqual(status["status"], "partial")
            self.assertLessEqual(len(rows), 1)
            self.assertEqual(status["budget"]["idsAccepted"], 1)


class SnapshotTests(unittest.TestCase):
    def test_complete_snapshot_has_valid_full_scoped_digests_and_counted_identity_checks(self):
        with RunningFakeQCE({"fetch": "dataset", "sameSecondPairs": 0}) as server, tempfile.TemporaryDirectory() as state:
            code, payload, out, err = run_tool(state, snapshot_args("before"), server.base)
            self.assertEqual(code, 0, out + err)
            stored = json.loads(Path(payload["written"]).read_text(encoding="utf-8"))
            self.assertEqual(stored["rowCount"], 12)
            self.assertEqual(len(stored["members"]), len(set(stored["members"])))
            self.assertTrue(all(len(value) == 64 for value in stored["members"]))
            self.assertEqual(stored["clientVersion"]["qceVersion"], "6.3.0-fake")
            self.assertEqual(stored["requestBudget"]["usedTotalRequests"], len(server.requests))
            self.assertEqual(tool.compare_snapshots(stored, stored)["status"], "ok")
            blob = out + err + json.dumps(stored)
            for private in (TOKEN, "7562417847123", '"10001"', PEER):
                self.assertNotIn(private, blob)

    def test_partial_window_cannot_produce_comparable_evidence(self):
        with RunningFakeQCE({"fetch": "dataset", "sameSecondPairs": 0}) as server, tempfile.TemporaryDirectory() as state:
            code, payload, out, err = run_tool(state, snapshot_args("small", "--max-pages", "1"), server.base)
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "refused")
            self.assertFalse((Path(state) / "cp06_small.json").exists())

    def test_identity_switch_during_scan_refuses_to_write(self):
        with RunningFakeQCE({"fetch": "dataset", "sameSecondPairs": 0, "selfUinAfter": "10002"}) as server, tempfile.TemporaryDirectory() as state:
            code, payload, out, err = run_tool(state, snapshot_args("switched"), server.base)
            self.assertEqual(code, 2)
            self.assertEqual(payload["failureStage"], "account")
            self.assertFalse((Path(state) / "cp06_switched.json").exists())

    def test_one_total_request_is_not_silently_expanded_for_versions_and_scan(self):
        with RunningFakeQCE({"fetch": "dataset"}) as server, tempfile.TemporaryDirectory() as state:
            code, payload, out, err = run_tool(state, snapshot_args("tiny", "--max-total-requests", "1",
                                                                 "--max-fetch-requests", "1"), server.base)
            self.assertEqual(code, 2)
            self.assertEqual(len(server.requests), 1)
            self.assertFalse((Path(state) / "cp06_tiny.json").exists())


class CompareTests(unittest.TestCase):
    def test_equal_sets_without_restart_only_claim_set_equality(self):
        verdict = tool.compare_snapshots(snapshot(["a", "b"]), snapshot(["b", "a"], at=3000))
        self.assertTrue(verdict["identicalSet"])
        self.assertEqual(verdict["restartEvidence"], "missing")
        self.assertIn("缺少重启步骤证据", verdict["conclusion"])
        self.assertNotIn("支持跨重启稳定", verdict["conclusion"])

    def test_explicit_restart_evidence_is_tied_to_scope_digest_and_time_interval(self):
        before, after = snapshot(["a"]), snapshot(["a"], at=3000)
        restart = {"kind": "user-confirmed", "scope": before["scope"],
                   "beforeSetDigest": before["setIdDigest"], "beforeTakenAtMs": 1000, "confirmedAtMs": 2000}
        verdict = tool.compare_snapshots(before, after, restart)
        self.assertEqual(verdict["restartEvidence"], "user-confirmed")
        self.assertIn("支持跨重启稳定", verdict["conclusion"])
        restart["confirmedAtMs"] = 5000
        self.assertEqual(tool.compare_snapshots(before, after, restart)["restartEvidence"], "missing")

    def test_equal_length_peers_accounts_salts_and_sessions_cannot_compare(self):
        before = snapshot(["a"])
        variants = [snapshot(["a"], peer="u_synthetic_peerX"),
                    snapshot(["a"], owner="10002"), snapshot(["a"], salt=b"y" * 32)]
        same_length = snapshot(["a"], peer=PEER[:-1] + "X")
        self.assertEqual(len(PEER), len(PEER[:-1] + "X"))
        variants.append(same_length)
        variant = copy.deepcopy(before)
        variant["scope"]["measurementSessionId"] = "0" * 64
        variants.append(variant)
        for after in variants:
            self.assertEqual(tool.compare_snapshots(before, after)["status"], "refused")

    def test_legacy_weak_scope_or_corrupted_count_and_digest_are_refused(self):
        before = snapshot(["a"])
        for field, value in (("schema", 1), ("rowCount", 2), ("setIdDigest", "0" * 64),
                             ("terminalStatus", "partial"), ("members", [])):
            after = {**before, field: value}
            self.assertEqual(tool.compare_snapshots(before, after)["status"], "refused")
        after = copy.deepcopy(before)
        del after["scope"]["accountDigest"]
        self.assertEqual(tool.compare_snapshots(before, after)["status"], "refused")

    def test_set_difference_lists_confounders_instead_of_blaming_ids(self):
        verdict = tool.compare_snapshots(snapshot(["a"]), snapshot(["a", "b"]))
        self.assertEqual((verdict["onlyAfter"], verdict["identicalSet"]), (1, False))
        self.assertIn("不能直接推断", verdict["confounders"][0])
        self.assertNotIn("conclusion", verdict)

    def test_attribute_changes_and_missing_window_cannot_yield_restart_acceptance(self):
        before, after = snapshot(["a"]), snapshot(["a"], at=3000)
        after["attributeShapes"] = {"recallTime": {"str/10": 1}}
        self.assertNotIn("conclusion", tool.compare_snapshots(before, after))
        del after["scope"]["windowStartMs"]
        self.assertEqual(tool.compare_snapshots(before, after)["status"], "refused")


class LatencyTests(unittest.TestCase):
    def test_single_unique_armed_action_has_an_interval(self):
        out = tool.correlate_latency([poll(at=2500)], [marker(at=1000)], 2)
        event = out["events"][0]
        self.assertEqual(event["latencyIntervalMs"], {"lower": 1400, "upper": 1500})
        self.assertEqual(out["eligibleSamples"], 1)
        self.assertFalse(out["acceptedAgainst8sTarget"])

    def test_one_event_cannot_become_twenty_samples(self):
        markers = [marker(i, at=1000) for i in range(20)]
        out = tool.correlate_latency([poll()], markers, 2)
        self.assertEqual(out["observed"], 0)
        self.assertFalse(out["acceptedAgainst8sTarget"])

    def test_partial_poll_and_later_duplicate_are_not_promoted_into_evidence(self):
        partial = poll(status="partial")
        later = copy.deepcopy(partial)
        later.update(status="complete", atMs=3000)
        later["events"][0]["observedAtMs"] = 3000
        out = tool.correlate_latency([partial, later], [marker()], 2)
        self.assertEqual(out["observed"], 0)

    def test_two_same_direction_events_are_ambiguous(self):
        one = poll()
        one["events"] += poll(1)["events"]
        out = tool.correlate_latency([one], [marker()], 2)
        self.assertEqual(out["events"][0]["reason"], "ambiguous-event-or-action")

    def test_post_action_only_marker_never_creates_a_latency_upper_bound(self):
        out = tool.correlate_latency([poll()], [marker(armed=False)], 2)
        event = out["events"][0]
        self.assertTrue(event["observed"])
        self.assertEqual(event["markerToFirstObservationMs"], 900)
        self.assertNotIn("latencyIntervalMs", event)
        self.assertFalse(event["eligibleForP95"])

    def test_wrong_run_scope_direction_and_count_only_polls_are_unmatched(self):
        wrong_scope = poll()
        wrong_scope["scopeDigest"] = "0" * 64
        for value in (poll(run_id="b" * 32), wrong_scope, poll(direction="other"),
                      {"kind": "poll", "atMs": 2000, "status": "complete", "newByDirection": {"self": 20}}):
            self.assertEqual(tool.correlate_latency([value], [marker()], 2)["observed"], 0)

    def test_actual_p95_not_all_samples_must_be_under_eight_seconds(self):
        polls, markers = [], []
        for i in range(20):
            start = i * 20000 + 1000
            markers.append(marker(i, at=start))
            polls.append(poll(i, at=start + (9000 if i == 19 else 1000)))
        out = tool.correlate_latency(polls, markers, 2)
        self.assertEqual(out["eligibleSamples"], 20)
        self.assertEqual(out["p95UpperBoundMs"], 1000)
        self.assertTrue(out["acceptedAgainst8sTarget"])
        self.assertFalse(tool.correlate_latency(polls, markers + [marker(99, at=9999999)], 2)["acceptedAgainst8sTarget"])

    def test_duplicate_action_id_is_not_counted_twice(self):
        values = [marker(at=1000), marker(at=4000)]
        out = tool.correlate_latency([poll(at=2000), poll(1, at=5000)], values, 2)
        self.assertEqual(out["eligibleSamples"], 0)


class CollectAndMarkerTests(unittest.TestCase):
    def collect(self, state, server, *extra):
        return run_tool(state, ["cp04-collect", "--peer-uid", PEER, "--duration-seconds", "0.5",
                               "--interval-seconds", "0.05", "--limit", "50", "--batch-size", "50",
                               "--max-total-requests", "12", "--max-fetch-requests", "8", *extra], server.base)

    def test_collector_baselines_first_ignores_old_markers_and_bounds_all_requests(self):
        with RunningFakeQCE({"fetch": "dataset", "arrivals": ["other", "self"]}) as server, tempfile.TemporaryDirectory() as state:
            (Path(state) / "cp04_markers.json").write_text(json.dumps([{"kind": "sent", "atMs": 1}]))
            code, payload, out, err = self.collect(state, server)
            self.assertEqual(code, 0, out + err)
            self.assertGreaterEqual(payload["baselineRows"], 1)
            self.assertGreaterEqual(payload["pollCount"], 1)
            self.assertEqual(payload["latency"]["marked"], 0)
            self.assertLessEqual(len(server.requests), 12)
            self.assertEqual(payload["requestBudget"]["usedTotalRequests"], len(server.requests))
            for private in (TOKEN, "7562417847123", '"10001"', PEER):
                self.assertNotIn(private, out + err)
            self.assertTrue((Path(state) / ("cp04_run_" + payload["runId"] + ".json")).is_file())

    def test_incomplete_baseline_refuses_to_start(self):
        with RunningFakeQCE({"fetch": "missing-metadata"}) as server, tempfile.TemporaryDirectory() as state:
            code, payload, out, err = self.collect(state, server)
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "refused")
            self.assertIn("baseline", payload["reason"])
            self.assertFalse((Path(state) / "cp04_active.json").exists())

    def test_arm_mark_and_report_use_the_same_run_without_network(self):
        with tempfile.TemporaryDirectory() as state:
            run = {"protocol": "CP04", "schema": 2, "runId": RUN, "scopeDigest": SCOPE["conversationDigest"],
                   "status": "running", "polls": [], "intervalSeconds": 2}
            tool._write_json(tool._run_path(state, RUN), run, new=True)
            tool._write_json(Path(state) / "cp04_active.json", {"runId": RUN})
            code, arm, _, err = run_tool(state, ["cp04-arm", "--kind", "received"])
            self.assertEqual(code, 0, err)
            code, mark, _, err = run_tool(state, ["cp04-mark"])
            self.assertEqual(code, 0, err)
            self.assertEqual(arm["actionId"], mark["actionId"])
            code, report, _, err = run_tool(state, ["cp04-report"])
            self.assertEqual(code, 0, err)
            self.assertEqual(report["latency"]["marked"], 1)
            self.assertEqual(report["latency"]["events"][0]["markerKind"], "received")
            self.assertFalse(report["latency"]["acceptedAgainst8sTarget"])

    def test_unfinished_armed_action_remains_visible_as_unmatched(self):
        with tempfile.TemporaryDirectory() as state:
            pending = marker(9)
            del pending["markedAtMs"]
            tool._write_json(Path(state) / ("cp04_" + RUN) / "pending.json", pending)
            markers = tool._load_markers(state, RUN)
            self.assertEqual(len(markers), 1)
            result = tool.correlate_latency([poll()], markers, 2)
            self.assertFalse(result["events"][0]["observed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

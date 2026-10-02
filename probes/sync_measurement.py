"""Bounded CP04/CP06 evidence collection; review 09 M01-M06.

Uses qce_probe.bounded_fetch, never a second pagination implementation.
Request/time limits cover the whole run, including identity/version checks.
Pages/messages/poll seconds limit each scan. Arm -> natural action -> mark
provides a bounded action interval; post-action markers do not prove latency.

PowerShell credential input:
  $secureToken = Read-Host 'QCE token' -AsSecureString
  $env:QCE_TOKEN = [System.Net.NetworkCredential]::new('', $secureToken).Password
  # Remove-Item Env:QCE_TOKEN after the authorized measurement.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "probes"))
sys.path.insert(0, str(REPO_ROOT / "bridge"))
import qce_probe
from qq_identity import canonical_uin

DIRECTION_SELF = "self"
DIRECTION_OTHER = "other"
TERMINAL_STATES = ("complete", "complete-empty")
DEFAULT_STATE_DIR = REPO_ROOT.parent / "QQVibeMeasure"
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
RUN_RE = re.compile(r"[0-9a-f]{32}\Z")
VERSION_RE = re.compile(r"(?:v|rust-)?\d{1,4}(?:\.\d{1,4}){1,3}(?:[-+][A-Za-z0-9.-]{1,40})?\Z")
_PRIVATE = set()


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


def resolve_state_dir(raw):
    path = Path(raw).expanduser().resolve()
    if path.is_relative_to(REPO_ROOT.resolve()) or any((base / ".git").exists() for base in (path, *path.parents)):
        die("refusing state directory inside the repository or another Git worktree")
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_or_create_salt(state_dir):
    path = Path(state_dir) / "measurement_salt.bin"
    try:
        with path.open("xb") as output:
            output.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    salt = path.read_bytes()
    if len(salt) != 32:
        raise qce_probe.ProbeFailure("config", "measurement salt is malformed")
    return salt


def _hmac(salt, domain, *parts):
    payload = json.dumps(["qqvibe-measure-v2", domain, *parts], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hmac.new(salt, payload, hashlib.sha256).hexdigest()


def measurement_scope(salt, account, peer):
    account = canonical_uin(account)
    return {"platform": "qq", "accountDigest": _hmac(salt, "account", "qq", account),
            "conversationDigest": _hmac(salt, "conversation", "qq", account, peer),
            "saltFingerprint": hashlib.sha256(b"qqvibe-measure-salt-v2\0" + salt).hexdigest(),
            "measurementSessionId": _hmac(salt, "measurement-session")}


def digest_id(salt, native_id, *, account, peer, native_kind="qce-msgId"):
    return _hmac(salt, "event", "qq", canonical_uin(account), peer, native_kind, native_id)


def set_digest(digests):
    return hashlib.sha256("\n".join(sorted(set(digests))).encode("ascii")).hexdigest()


def _write_json(path, value, *, new=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if new:
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
        return
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise qce_probe.ProbeFailure("config", "measurement state exceeds its size limit")
    return json.loads(raw)


def make_probe_args(peer_uid, *, batch_size, limit, max_pages, max_messages, max_fetch_requests,
                    max_total_requests, request_timeout, max_seconds, max_response_bytes):
    return types.SimpleNamespace(**locals())


def _budget(args, duration, *, parent=None):
    return qce_probe.Budget(max_total_requests=args.max_total_requests,
                            max_fetch_requests=args.max_fetch_requests,
                            max_pages=args.max_pages, max_messages=args.max_messages,
                            max_seconds=duration, request_timeout=args.request_timeout, parent=parent)


def _run_client(args, token, duration):
    budget = _budget(args, duration)
    return qce_probe.Client(qce_probe.validate_base(args.base), token, budget,
                            max_response_bytes=args.max_response_bytes)


def _run_counts(budget):
    return {key: value for key, value in budget.snapshot().items()
            if key not in {"maxPages", "maxMessages", "pagesRecorded", "idsAccepted"}}


def _token(args):
    token = qce_probe.load_token(args.token_file)[0]
    _PRIVATE.add(token)
    return token


def _identity(client):
    _, payload, _ = client.request("get", "GET", "/api/system/info", "GET system/info")
    info, _ = qce_probe.unwrap_envelope(payload, "GET system/info")
    napcat = info.get("napcat")
    self_info = napcat.get("selfInfo") if isinstance(napcat, dict) else None
    try:
        owner = canonical_uin(self_info.get("uin") if isinstance(self_info, dict) else None)
    except ValueError:
        raise qce_probe.ProbeFailure("protocol", "system/info has no usable self identity") from None
    return owner, info


def resolve_self_uin(base, token, *, client=None):
    if client is None:
        budget = qce_probe.Budget(max_total_requests=1, max_fetch_requests=1, max_pages=1,
                                  max_messages=1, max_seconds=10, request_timeout=5)
        client = qce_probe.Client(qce_probe.validate_base(base), token, budget,
                                  max_response_bytes=qce_probe.DEFAULT_MAX_RESPONSE_BYTES)
    return _identity(client)[0]


def _version_of(base, token, *, client=None, info=None):
    if client is None:
        budget = qce_probe.Budget(max_total_requests=2, max_fetch_requests=1, max_pages=1,
                                  max_messages=1, max_seconds=10, request_timeout=5)
        client = qce_probe.Client(qce_probe.validate_base(base), token, budget,
                                  max_response_bytes=qce_probe.DEFAULT_MAX_RESPONSE_BYTES)
    if info is None:
        _, payload, _ = client.request("get", "GET", "/api/system/info", "GET system/info")
        info, _ = qce_probe.unwrap_envelope(payload, "GET system/info")
    _, payload, _ = client.request("get", "GET", "/api/system/status", "GET system/status")
    status, _ = qce_probe.unwrap_envelope(payload, "GET system/status")

    def version(value):
        return value if isinstance(value, str) and VERSION_RE.fullmatch(value) and token not in value else None

    napcat = info.get("napcat") if isinstance(info.get("napcat"), dict) else {}
    runtime = info.get("runtime") if isinstance(info.get("runtime"), dict) else {}
    build = info.get("commit") or info.get("buildHash")
    return {"qceVersion": version(info.get("version")), "napcatVersion": version(napcat.get("version")),
            "runtimeVersion": version(runtime.get("nodeVersion")),
            "buildHash": build if isinstance(build, str) and re.fullmatch(r"[0-9a-f]{7,40}", build) and token not in build else None,
            "mode": info.get("mode") if info.get("mode") in ("plugin", "standalone") else None,
            "loggedIn": status.get("loggedIn") if type(status.get("loggedIn")) is bool else napcat.get("online") if type(napcat.get("online")) is bool else None}


def fetch_with_rows(base, token, salt, self_uin, probe_args, window, *, run_client=None):
    """One shared scanner; accepted raw rows only exist inside this callback."""
    budget = _budget(probe_args, probe_args.max_seconds,
                     parent=run_client.budget if run_client is not None else None)
    client = qce_probe.Client(qce_probe.validate_base(base), token, budget,
                              max_response_bytes=probe_args.max_response_bytes)
    rows = []

    def observe(items, page):
        at_ms = int(time.time() * 1000)
        for item in items:
            try:
                sender = canonical_uin(item.get("senderUin"))
                direction = DIRECTION_SELF if sender == canonical_uin(self_uin) else DIRECTION_OTHER
            except ValueError:
                direction = "unknown"
            send_type = (str(item["sendType"]).lstrip("0") or "0") if item.get("sendType") is not None else None
            if send_type == "3":
                direction = "system"
            elif (send_type == "2" and direction != DIRECTION_SELF or
                  send_type == "0" and direction == DIRECTION_SELF):
                direction = "conflict"
            rows.append({"digest": digest_id(salt, item["msgId"], account=self_uin, peer=probe_args.peer_uid),
                         "direction": direction, "observedAtMs": at_ms,
                         "requestDurationMs": page["elapsedMs"],
                         "timeShape": qce_probe.value_shape(item.get("msgTime")),
                         "recallShape": "absent" if "recallTime" not in item else
                         json.dumps(qce_probe.value_shape(item["recallTime"]), sort_keys=True)})

    try:
        result = qce_probe.bounded_fetch(client, probe_args, window, on_rows=observe)
        return {key: result[key] for key in ("status", "partialReason", "requestsUsed", "budget")}, rows
    except qce_probe.ProbeFailure as exc:
        return {"status": "error", "failureStage": exc.stage, "failureDetail": exc.detail,
                "requestsUsed": budget.used_fetch, "budget": budget.snapshot()}, rows


def correlate_latency(polls, markers, interval_seconds):
    """Match unique event IDs one-to-one; reject partial, ambiguous or cross-run evidence."""
    first_seen = {}
    for poll in sorted(polls, key=lambda value: value.get("atMs", 0)):
        if poll.get("kind") != "poll":
            continue
        for event in poll.get("events", []):
            identifier = event.get("eventId")
            if isinstance(identifier, str) and DIGEST_RE.fullmatch(identifier):
                first_seen.setdefault((poll.get("runId"), poll.get("scopeDigest"), identifier), (poll, event))
    candidates = []
    for marker in markers:
        at_ms = marker.get("armedAtMs", marker.get("markedAtMs"))
        wanted = DIRECTION_SELF if marker.get("kind") == "sent" else DIRECTION_OTHER
        matching = []
        if (marker.get("kind") in ("sent", "received") and type(at_ms) is int and
                type(marker.get("markedAtMs")) is int and at_ms <= marker["markedAtMs"]):
            for (run_id, scope, identifier), (poll, event) in first_seen.items():
                if (run_id != marker.get("runId") or scope != marker.get("scopeDigest") or
                        not run_id or not scope or poll.get("status") not in TERMINAL_STATES or
                        event.get("direction") != wanted or type(event.get("observedAtMs")) is not int):
                    continue
                if at_ms <= event["observedAtMs"] <= marker["markedAtMs"] + 60000:
                    matching.append((poll, event))
        if matching:
            first_poll = min(item[0]["atMs"] for item in matching)
            matching = [item for item in matching if item[0]["atMs"] == first_poll]
        candidates.append(matching)
    claims = {}
    for matches in candidates:
        for poll, event in matches:
            key = (poll.get("runId"), poll.get("scopeDigest"), event["eventId"])
            claims[key] = claims.get(key, 0) + 1
    action_counts = {}
    for marker in markers:
        action_counts[marker.get("actionId")] = action_counts.get(marker.get("actionId"), 0) + 1
    events = []
    for marker, matches in zip(markers, candidates):
        entry = {"actionId": marker.get("actionId"), "markerKind": marker.get("kind"),
                 "observed": False, "eligibleForP95": False}
        if not matches:
            entry["reason"] = "no-unique-complete-observation"
        elif (len(matches) != 1 or not marker.get("actionId") or action_counts[marker["actionId"]] != 1 or
              claims[(matches[0][0].get("runId"), matches[0][0].get("scopeDigest"), matches[0][1]["eventId"])] != 1):
            entry["reason"] = "ambiguous-event-or-action"
        else:
            poll, event = matches[0]
            observed = event["observedAtMs"]
            entry.update({"observed": True, "eventId": event["eventId"], "firstVisibleAtMs": observed,
                          "markerToFirstObservationMs": observed - marker["markedAtMs"],
                          "pollIntervalMs": int(interval_seconds * 1000)})
            if type(marker.get("armedAtMs")) is int:
                entry["latencyIntervalMs"] = {"lower": max(0, observed - marker["markedAtMs"]),
                                             "upper": observed - marker["armedAtMs"]}
                entry["eligibleForP95"] = True
            else:
                entry["reason"] = "post-action-marker-only-not-a-latency-bound"
        events.append(entry)
    values = sorted(item["latencyIntervalMs"]["upper"] for item in events if item["eligibleForP95"])
    p95 = values[math.ceil(.95 * len(values)) - 1] if values else None
    return {"events": events, "marked": len(markers), "observed": sum(item["observed"] for item in events),
            "eligibleSamples": len(values), "p95UpperBoundMs": p95,
            "acceptedAgainst8sTarget": len(values) >= 20 and len(values) == len(markers) and p95 <= 8000,
            "metric": "nearest-rank P95 of arm-to-first-observation upper bounds",
            "note": "Only unique armed actions in complete polls qualify. Missing/partial/ambiguous events are not successful samples. This measures QCE visibility, not the desktop UI."}


def _run_path(state, run_id):
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise qce_probe.ProbeFailure("config", "invalid measurement run id")
    return Path(state) / ("cp04_run_" + run_id + ".json")


def _current_run(args):
    run_id = getattr(args, "run_id", None)
    if not run_id:
        run_id = _read_json(Path(args.state_dir) / "cp04_active.json").get("runId")
    run = _read_json(_run_path(args.state_dir, run_id))
    if run.get("runId") != run_id:
        raise qce_probe.ProbeFailure("config", "run metadata mismatch")
    return run


def _load_markers(state, run_id):
    directory = Path(state) / ("cp04_" + run_id) / "markers"
    markers = [_read_json(path) for path in directory.glob("*.json")] if directory.is_dir() else []
    pending = directory.parent / "pending.json"
    if pending.exists():
        markers.append(_read_json(pending))
    return sorted(markers,
                  key=lambda value: value.get("armedAtMs", value.get("markedAtMs", 0)))


def cmd_cp04_marker(args):
    run = _current_run(args)
    directory = Path(args.state_dir) / ("cp04_" + run["runId"])
    pending = directory / "pending.json"
    if args.command == "cp04-arm":
        if pending.exists():
            raise qce_probe.ProbeFailure("config", "an armed action still needs its mark")
        if run.get("status") != "running":
            raise qce_probe.ProbeFailure("config", "start an active collector before arming")
        marker = {"actionId": secrets.token_hex(16), "runId": run["runId"], "scopeDigest": run["scopeDigest"],
                  "kind": args.kind, "armedAtMs": int(time.time() * 1000)}
        _write_json(pending, marker, new=True)
        _emit({"status": "armed", "runId": run["runId"], "actionId": marker["actionId"], "kind": args.kind})
    else:
        marker = _read_json(pending) if pending.exists() else {
            "actionId": secrets.token_hex(16), "runId": run["runId"], "scopeDigest": run["scopeDigest"], "kind": args.kind}
        marker["markedAtMs"] = int(time.time() * 1000)
        _write_json(directory / "markers" / (marker["actionId"] + ".json"), marker, new=True)
        pending.unlink(missing_ok=True)
        _emit({"status": "marked", "runId": run["runId"], "actionId": marker["actionId"],
               "armed": "armedAtMs" in marker})
    return 0


def cmd_cp04_report(args):
    run = _current_run(args)
    _emit({**run, "pollCount": len(run["polls"]),
           "doesNotProve": ["计数增加本身不能证明指定动作；需要同运行、同范围的唯一事件匹配。",
                            "QCE 可见性不是 QQVibe 界面端到端同步延迟；标记后观察间隔也不是发送延迟上界。"],
           "latency": correlate_latency(run["polls"], _load_markers(args.state_dir, run["runId"]),
                                               run["intervalSeconds"])})
    return 0


def cmd_cp04_collect(args):
    token, state = _token(args), Path(args.state_dir)
    salt = load_or_create_salt(state)
    client = _run_client(args, token, args.duration_seconds)
    owner, info = _identity(client)
    versions = _version_of(args.base, token, client=client, info=info)
    scope = measurement_scope(salt, owner, args.peer_uid)
    probe_args = make_probe_args(args.peer_uid, batch_size=args.batch_size, limit=args.limit,
        max_pages=args.max_pages, max_messages=args.max_messages, max_fetch_requests=args.max_fetch_requests,
        max_total_requests=args.max_total_requests, request_timeout=args.request_timeout,
        max_seconds=args.poll_max_seconds, max_response_bytes=args.max_response_bytes)

    def scan():
        if _identity(client)[0] != owner:
            raise qce_probe.ProbeFailure("account", "account changed during measurement")
        end = int(time.time() * 1000)
        result, rows = fetch_with_rows(args.base, token, salt, owner, probe_args,
                                      (end - int(args.window_minutes * 60000), end), run_client=client)
        if _identity(client)[0] != owner:
            raise qce_probe.ProbeFailure("account", "account changed during measurement")
        return result, rows

    baseline, rows = scan()
    if baseline["status"] not in TERMINAL_STATES:
        _emit({"status": "refused", "reason": "baseline was not fully read", "detail": baseline})
        return 2
    known = {row["digest"] for row in rows}
    run_id = args.run_id or secrets.token_hex(16)
    run = {"protocol": "CP04", "schema": 2, "runId": run_id, "scope": scope,
           "scopeDigest": scope["conversationDigest"], "startedAtMs": int(time.time() * 1000),
           "status": "running", "clientVersion": versions, "intervalSeconds": args.interval_seconds,
           "baselineRows": len(rows), "polls": [], "requestBudget": _run_counts(client.budget)}
    path = _run_path(state, run_id)
    _write_json(path, run, new=True)
    _write_json(state / "cp04_active.json", {"runId": run_id})
    _emit({"status": "collecting", "runId": run_id, "baselineRows": len(rows)})
    try:
        while client.budget.time_left() > args.interval_seconds:
            time.sleep(args.interval_seconds)
            status, rows = scan()
            fresh = [row for row in rows if row["digest"] not in known]
            known.update(row["digest"] for row in rows)
            if len(known) > args.max_events:
                raise qce_probe.BudgetExhausted("max-events budget reached")
            run["polls"].append({"kind": "poll", "runId": run_id, "scopeDigest": run["scopeDigest"],
                                 "atMs": int(time.time() * 1000), "status": status["status"],
                                 "requestsUsed": status["requestsUsed"],
                                 "events": [{"eventId": row["digest"], "direction": row["direction"],
                                             "observedAtMs": row["observedAtMs"]} for row in fresh]})
            run["requestBudget"] = _run_counts(client.budget)
            _write_json(path, run)
            if status["status"] == "error":
                run["stopReason"] = "scan-error"
                break
    except qce_probe.BudgetExhausted as exc:
        run["stopReason"] = exc.reason
    except qce_probe.ProbeFailure as exc:
        run["stopReason"] = exc.stage
    run["status"] = "finished"
    run["finishedAtMs"] = int(time.time() * 1000)
    run["requestBudget"] = _run_counts(client.budget)
    _write_json(path, run)
    args.run_id = run_id
    return cmd_cp04_report(args)


def _attribute_shapes(rows):
    times, recalls = {}, {}
    for row in rows:
        key = f"{row['timeShape'].get('type')}/{row['timeShape'].get('length', '')}"
        times[key] = times.get(key, 0) + 1
        recalls[row["recallShape"]] = recalls.get(row["recallShape"], 0) + 1
    return {"msgTime": times, "recallTime": recalls}


def cmd_cp06_snapshot(args):
    token, state = _token(args), Path(args.state_dir)
    salt = load_or_create_salt(state)
    client = _run_client(args, token, args.max_seconds)
    owner, info = _identity(client)
    versions = _version_of(args.base, token, client=client, info=info)
    result, rows = fetch_with_rows(args.base, token, salt, owner, args,
                                  (args.window_start_ms, args.window_end_ms), run_client=client)
    if _identity(client)[0] != owner:
        raise qce_probe.ProbeFailure("account", "account changed during measurement")
    if result["status"] not in TERMINAL_STATES or not rows:
        _emit({"status": "refused", "reason": "window read was " + result["status"] if rows else "empty or incomplete window",
               "detail": result.get("partialReason")})
        return 2
    members = sorted(row["digest"] for row in rows)
    scope = {**measurement_scope(salt, owner, args.peer_uid),
             "windowStartMs": args.window_start_ms, "windowEndMs": args.window_end_ms}
    snapshot = {"protocol": "CP06", "schema": 2, "label": args.label, "takenAtMs": int(time.time() * 1000),
                "scope": scope, "terminalStatus": result["status"], "rowCount": len(members),
                "setIdDigest": set_digest(members), "members": members,
                "attributeShapes": _attribute_shapes(rows), "clientVersion": versions,
                "requestBudget": _run_counts(client.budget),
                "privacy": {"noBody": True, "noNickname": True, "noRawId": True, "noToken": True,
                            "noSelfUin": True, "saltNotExported": True}}
    output = state / ("cp06_" + args.label + ".json")
    _write_json(output, snapshot, new=True)
    _emit({"status": "ok", "written": str(output), "rowCount": len(members),
           "setIdDigest": snapshot["setIdDigest"], "requestBudget": snapshot["requestBudget"]})
    return 0


def compare_snapshots(before, after, restart=None):
    problems = []
    if any(not isinstance(snapshot, dict) or not isinstance(snapshot.get("scope"), dict)
           for snapshot in (before, after)):
        return {"status": "refused", "reasons": ["invalid snapshot shape"]}
    for name, snapshot in (("before", before), ("after", after)):
        scope, members = snapshot.get("scope", {}), snapshot.get("members", [])
        if (snapshot.get("protocol") != "CP06" or type(snapshot.get("takenAtMs")) is not int or
                type(scope.get("windowStartMs")) is not int or type(scope.get("windowEndMs")) is not int or
                not 0 <= scope.get("windowStartMs", -1) < scope.get("windowEndMs", -1)):
            problems.append(name + " has invalid window/time evidence")
        if snapshot.get("schema") != 2 or scope.get("platform") != "qq" or any(
                not isinstance(scope.get(key), str) or not DIGEST_RE.fullmatch(scope[key])
                for key in ("accountDigest", "conversationDigest", "saltFingerprint", "measurementSessionId")):
            problems.append(name + " has missing identity/salt scope")
        if snapshot.get("terminalStatus") not in TERMINAL_STATES:
            problems.append(name + " was not fully read")
        if not members:
            problems.append(name + " is empty")
        if (not isinstance(members, list) or any(not isinstance(value, str) or not DIGEST_RE.fullmatch(value) for value in members)
                or len(set(members)) != len(members) or type(snapshot.get("rowCount")) is not int or
                len(members) != snapshot.get("rowCount")
                or set_digest(members) != snapshot.get("setIdDigest")):
            problems.append(name + " has invalid member evidence")
    if before.get("scope") != after.get("scope"):
        problems.append("scope differs (same account, conversation, salt, session and window required)")
    if problems:
        return {"status": "refused", "reasons": problems}
    left, right = set(before["members"]), set(after["members"])
    restart_confirmed = bool(isinstance(restart, dict) and restart.get("kind") == "user-confirmed" and
                             restart.get("scope") == before["scope"] and
                             restart.get("beforeSetDigest") == before["setIdDigest"] and
                             restart.get("beforeTakenAtMs") == before["takenAtMs"] and
                             type(restart.get("confirmedAtMs")) is int and
                             before["takenAtMs"] <= restart["confirmedAtMs"] <= after["takenAtMs"])
    same = left == right
    verdict = {"status": "ok", "intersection": len(left & right), "onlyBefore": len(left - right),
               "onlyAfter": len(right - left), "identicalSet": same,
               "rowCountBefore": len(left), "rowCountAfter": len(right),
               "attributeShapesChanged": before["attributeShapes"] != after["attributeShapes"],
               "restartEvidence": "user-confirmed" if restart_confirmed else "missing", "confounders": []}
    if not same:
        verdict["confounders"].append("集合不等不能直接推断 ID 不稳定；新增、撤回或回补也能改变集合。")
    if before.get("clientVersion") != after.get("clientVersion"):
        verdict["confounders"].append("版本组合变化，不能沿用同版本结论。")
    if verdict["attributeShapesChanged"]:
        verdict["confounders"].append("消息属性形态变化，先核对撤回或格式差异，再讨论重启稳定性。")
    if same and not verdict["confounders"]:
        verdict["conclusion"] = ("有用户确认的重启步骤，且同一核验范围的 ID 集合相等；本轮支持跨重启稳定，范围仅限该窗口与版本组合。"
                                 if restart_confirmed else "两次已核验相同范围的 ID 集合相等；缺少重启步骤证据。")
    return verdict


def cmd_cp06_restart_mark(args):
    before = _read_json(Path(args.state_dir) / ("cp06_" + args.label_before + ".json"))
    if compare_snapshots(before, before)["status"] != "ok":
        raise qce_probe.ProbeFailure("config", "before snapshot is not valid restart evidence")
    evidence = {"kind": "user-confirmed", "scope": before["scope"],
                "beforeSetDigest": before["setIdDigest"], "beforeTakenAtMs": before["takenAtMs"],
                "confirmedAtMs": int(time.time() * 1000)}
    _write_json(Path(args.state_dir) / ("cp06_restart_" + args.label_before + ".json"), evidence, new=True)
    _emit({"status": "recorded", "evidenceClass": "user-confirmed"})
    return 0


def cmd_cp06_compare(args):
    state = Path(args.state_dir)
    before = _read_json(state / ("cp06_" + args.label_before + ".json"))
    after = _read_json(state / ("cp06_" + args.label_after + ".json"))
    path = state / ("cp06_restart_" + args.label_before + ".json")
    verdict = compare_snapshots(before, after, _read_json(path) if path.exists() else None)
    _emit(verdict)
    return 0 if verdict["status"] == "ok" else 2


def _emit(payload):
    encoded = json.dumps(payload, ensure_ascii=False)
    for token in _PRIVATE:
        if token:
            encoded = encoded.replace(json.dumps(token, ensure_ascii=False)[1:-1], "[redacted]")
    print("MEASURE_JSON=" + encoded, flush=True)


def build_parser():
    parser = qce_probe.SafeArgumentParser(description="CP04/CP06 bounded measurement", allow_abbrev=False)
    parser.add_argument("--base", default=os.environ.get("QCE_BASE", "http://127.0.0.1:40653"))
    parser.add_argument("--token-file", default="")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)
    common = qce_probe.SafeArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--peer-uid", required=True)
    for name, value in (("batch-size", 200), ("limit", 50), ("max-pages", 20), ("max-messages", 5000),
                        ("max-fetch-requests", 128), ("max-total-requests", 256),
                        ("max-response-bytes", qce_probe.DEFAULT_MAX_RESPONSE_BYTES)):
        common.add_argument("--" + name, type=int, default=value)
    common.add_argument("--request-timeout", type=float, default=10)
    collect = sub.add_parser("cp04-collect", parents=[common], allow_abbrev=False)
    collect.add_argument("--run-id")
    for name, value in (("window-minutes", 5), ("interval-seconds", 2), ("duration-seconds", 120), ("poll-max-seconds", 8)):
        collect.add_argument("--" + name, type=float, default=value)
    collect.add_argument("--max-events", type=int, default=20000)
    for command in ("cp04-arm", "cp04-mark", "cp04-report"):
        child = sub.add_parser(command, allow_abbrev=False)
        child.add_argument("--run-id")
        if command != "cp04-report":
            child.add_argument("--kind", choices=("sent", "received"), default="sent")
    snap = sub.add_parser("cp06-snapshot", parents=[common], allow_abbrev=False)
    snap.add_argument("--window-start-ms", type=int, required=True)
    snap.add_argument("--window-end-ms", type=int, required=True)
    snap.add_argument("--label", required=True)
    snap.add_argument("--max-seconds", type=float, default=30)
    restart = sub.add_parser("cp06-restart-mark", allow_abbrev=False)
    restart.add_argument("--label-before", required=True)
    compare = sub.add_parser("cp06-compare", allow_abbrev=False)
    compare.add_argument("--label-before", required=True)
    compare.add_argument("--label-after", required=True)
    return parser


def _validate(args):
    qce_probe.validate_base(args.base)
    for name in ("batch_size", "limit", "max_pages", "max_messages", "max_fetch_requests",
                 "max_total_requests", "max_response_bytes", "max_events"):
        if hasattr(args, name):
            qce_probe.positive_int(name, getattr(args, name), maximum=5000 if name == "batch_size" else None)
    for name in ("request_timeout", "window_minutes", "interval_seconds", "duration_seconds", "poll_max_seconds", "max_seconds"):
        if hasattr(args, name):
            qce_probe.positive_finite(name, getattr(args, name))
    if hasattr(args, "max_fetch_requests") and args.max_fetch_requests > args.max_total_requests:
        raise qce_probe.ProbeFailure("config", "fetch request limit exceeds the run request limit")
    for name in ("label", "label_before", "label_after"):
        if hasattr(args, name) and not LABEL_RE.fullmatch(getattr(args, name)):
            raise qce_probe.ProbeFailure("config", "label must be a safe slug")
    if getattr(args, "run_id", None) is not None and not RUN_RE.fullmatch(args.run_id):
        raise qce_probe.ProbeFailure("config", "invalid measurement run id")
    if hasattr(args, "window_start_ms") and not 0 <= args.window_start_ms < args.window_end_ms:
        raise qce_probe.ProbeFailure("config", "invalid measurement window")
    if hasattr(args, "peer_uid") and not re.fullmatch(r"u_[A-Za-z0-9_-]{1,128}", args.peer_uid):
        raise qce_probe.ProbeFailure("config", "invalid peer uid")


def main(argv=None):
    _PRIVATE.clear()
    args = build_parser().parse_args(argv)
    try:
        _validate(args)
        args.state_dir = str(resolve_state_dir(args.state_dir))
        handlers = {"cp04-collect": cmd_cp04_collect, "cp04-arm": cmd_cp04_marker,
                    "cp04-mark": cmd_cp04_marker, "cp04-report": cmd_cp04_report,
                    "cp06-snapshot": cmd_cp06_snapshot, "cp06-compare": cmd_cp06_compare,
                    "cp06-restart-mark": cmd_cp06_restart_mark}
        return handlers[args.command](args)
    except qce_probe.ProbeFailure as exc:
        _emit({"status": "error", "failureStage": exc.stage, "failureDetail": exc.detail})
    except qce_probe.BudgetExhausted as exc:
        _emit({"status": "refused", "reason": exc.reason})
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        _emit({"status": "error", "failureStage": "config", "failureDetail": type(exc).__name__})
    return 2


if __name__ == "__main__":
    sys.exit(main())

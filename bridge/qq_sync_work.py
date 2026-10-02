"""Versioned auxiliary history work; canonical message DDL and tail cursor stay unchanged."""
from __future__ import annotations

import copy
import json
import re

TABLE = "qq_sync_work_v1"
DDL = """CREATE TABLE qq_sync_work_v1 (
 account_key TEXT NOT NULL,
 conversation_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('history','reconcile')),
 revision INTEGER NOT NULL CHECK (revision >= 1),
 payload TEXT NOT NULL,
 PRIMARY KEY (account_key,conversation_key,kind),
 FOREIGN KEY (account_key,conversation_key) REFERENCES conversations(account_key,conversation_key)
)"""
KINDS = {"history", "reconcile"}
FIELDS = {"version", "anchorEndMs", "coverageStartMs", "window", "stack", "attempts",
          "state", "reason", "completedWindows", "lastCompletedAtMs", "lastWindow"}


def exists(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def validate_table(connection):
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    normalize = lambda sql: re.sub(r"\s+", " ", sql.strip().rstrip(";")).casefold()
    if row is None or normalize(row[0]) != normalize(DDL):
        raise ValueError("sync-work-schema-invalid")


def validate(kind, value):
    if kind not in KINDS or not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("sync-work-invalid")
    for name in ("version", "anchorEndMs", "coverageStartMs", "attempts", "completedWindows", "lastCompletedAtMs"):
        if type(value[name]) is not int or value[name] < 0:
            raise ValueError("sync-work-invalid")
    if value["version"] != 1 or value["coverageStartMs"] > value["anchorEndMs"] or value["attempts"] > 100:
        raise ValueError("sync-work-invalid")
    if value["state"] not in {"pending", "partial", "error", "complete", "split"}:
        raise ValueError("sync-work-invalid")
    reason = value["reason"]
    if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r"[a-z-]{1,64}", reason)):
        raise ValueError("sync-work-invalid")
    def interval(item):
        return isinstance(item, list) and len(item) == 2 and all(type(number) is int and number >= 0 for number in item) and item[0] <= item[1] <= value["anchorEndMs"]
    if value["window"] is not None and not interval(value["window"]):
        raise ValueError("sync-work-invalid")
    if value["lastWindow"] is not None and not interval(value["lastWindow"]):
        raise ValueError("sync-work-invalid")
    if not isinstance(value["stack"], list) or len(value["stack"]) > 64 or any(not interval(item) for item in value["stack"]):
        raise ValueError("sync-work-invalid")
    if kind == "history":
        end = 0
        for item in value["stack"] + ([value["window"]] if value["window"] is not None else []):
            if item[0] != end:
                raise ValueError("sync-work-gap")
            end = item[1]
        if end != value["coverageStartMs"]:
            raise ValueError("sync-work-gap")
        if value["state"] == "complete" and (value["window"] is not None or value["stack"] or value["coverageStartMs"] != 0):
            raise ValueError("sync-work-invalid")
    elif value["stack"]:
        raise ValueError("sync-work-invalid")
    return value


def initial(kind, anchor_end, start=0):
    if kind not in KINDS or type(anchor_end) is not int or anchor_end < 0 or type(start) is not int or not 0 <= start <= anchor_end:
        raise ValueError("sync-work-invalid")
    value = {"version": 1, "anchorEndMs": anchor_end, "coverageStartMs": anchor_end,
             "window": [0 if kind == "history" else start, anchor_end], "stack": [],
             "attempts": 0, "state": "pending", "reason": None, "completedWindows": 0,
             "lastCompletedAtMs": 0, "lastWindow": None}
    if kind == "history" and anchor_end == 0:
        value.update(window=None, state="complete")
    return validate(kind, value)


def advance(kind, previous, status, reason, now_ms, *, max_attempts=3, minimum_window_ms=1000, split_at=None):
    value = copy.deepcopy(validate(kind, previous))
    window = value["window"]
    if window is None:
        return value
    if status in ("complete", "complete-empty"):
        value.update(attempts=0, reason=None, completedWindows=value["completedWindows"] + 1,
                     lastCompletedAtMs=now_ms, lastWindow=window)
        if kind == "history":
            value["coverageStartMs"] = window[0]
            value["window"] = value["stack"].pop() if value["stack"] else None
        else:
            value["coverageStartMs"] = window[0]
            value["window"] = None
        value["state"] = "pending" if value["window"] is not None else "complete"
    else:
        value["attempts"] += int(reason != "request-budget-exhausted")
        value["state"] = "error" if status == "error" else "partial"
        value["reason"] = reason or "source-partial"
        if (kind == "history" and reason == "budget-exhausted" and value["attempts"] >= max_attempts and
                window[1] - window[0] >= 2 * minimum_window_ms):
            middle = ((window[0] + window[1]) // (2 * minimum_window_ms)) * minimum_window_ms
            if type(split_at) is int and window[0] + minimum_window_ms <= split_at <= window[1] - minimum_window_ms:
                middle = (split_at // minimum_window_ms) * minimum_window_ms
            if window[0] < middle < window[1] and len(value["stack"]) < 64:
                value["stack"].append([window[0], middle])
                value.update(window=[middle, window[1]], attempts=0, state="split", reason="window-split")
    return validate(kind, value)


def write(cursor, account, conversation, kind, expected_revision, payload):
    validate_table(cursor)
    validate(kind, payload)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("sync-work-invalid")
    row = cursor.execute("SELECT revision FROM qq_sync_work_v1 WHERE account_key=? AND conversation_key=? AND kind=?",
                         (account, conversation, kind)).fetchone()
    if (row[0] if row else 0) != expected_revision:
        raise ValueError("stale-sync-work")
    cursor.execute("INSERT INTO qq_sync_work_v1 VALUES (?,?,?,?,?) ON CONFLICT(account_key,conversation_key,kind) "
        "DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
        (account, conversation, kind, expected_revision + 1, json.dumps(payload, separators=(",", ":"))))

"""Read-only local scope and fixed-field diagnostics; never export source bodies."""
from datetime import datetime, timezone
from contextlib import contextmanager
from itertools import islice
import platform
import re
import struct
import sys

from qq_message_store import ANALYSIS_SQL

SYNC_STATES = {"online", "fetching", "partial", "offline", "paused", "disabled", "unavailable"}
WORK_STATES = {"pending", "partial", "error", "complete", "split", "complete-empty"}
REASONS = {"connector-awaiting-validation", "configuration-invalid", "configuration-unavailable",
    "credential-storage-unavailable", "auth-required", "account-changed", "qq-offline",
    "connection-unavailable", "unsupported-version",'unverified-version', "protocol-invalid", "source-rejected",
    "rate-limited", "request-budget-exhausted", "budget-exhausted", "window-split",
    "normalization-rejected", "pagination-missing", "page-repeated", "scope-changed",
    "sync-stopping", "source-partial", "cancelled",'conversation-read-paused'}


def mapping(value):
    return value if type(value) is dict else {}


def choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def count(value):
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None


def reason(value):
    return None if value is None else choice(value, REASONS)


@contextmanager
def read_snapshot(connection):
    started = not connection.in_transaction
    if started:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if started and connection.in_transaction:
            connection.rollback()


def local_scope(store, account, user):
    """Called inside QQSource.read_library; does not alter schema or checkpoints."""
    with read_snapshot(store.connection):
        return _local_scope(store, account, user)


def _local_scope(store, account, user):
    row = store.connection.execute(
        "SELECT data_revision FROM conversations WHERE account_key=? AND conversation_key=?",
        (account, user)).fetchone()
    if row is None:
        raise ValueError("unknown-local-conversation")
    totals = store.connection.execute("SELECT COUNT(*) AS total, MIN(time_ms) AS first, MAX(time_ms) AS last,"
        " COALESCE(SUM(" + ANALYSIS_SQL + "),0) AS valid,"
        " COALESCE(SUM(direction='peer' AND " + ANALYSIS_SQL + "),0) AS peer"
        " FROM messages WHERE account_key=? AND conversation_key=?", (account, user)).fetchone()
    versions = store.connection.execute("SELECT normalize_version,COUNT(*) FROM messages"
        " WHERE account_key=? AND conversation_key=? GROUP BY normalize_version ORDER BY normalize_version LIMIT 17",
        (account, user)).fetchall()
    checkpoint = store.checkpoint(account, user)
    forward = None if checkpoint is None else {
        "state": choice(checkpoint["last_task_status"], WORK_STATES),
        "windowStartMs": checkpoint["window_start_ms"], "windowEndMs": checkpoint["window_end_ms"],
        "scannedThroughMs": checkpoint["scanned_through_ms"], "reason": reason(checkpoint["partial_reason"])}
    work = {}
    for kind in ("history", "reconcile"):
        saved = store.sync_work(account, user, kind)
        if saved is not None:
            payload = saved["payload"]
            work[kind] = {name: payload[name] for name in (
                "state", "anchorEndMs", "coverageStartMs", "window", "stack", "lastCompletedAtMs", "lastWindow")}
            work[kind]["reason"] = reason(payload["reason"])
    from qq_ingest_audit import summary
    provenance = summary(store.connection, account, user)
    provenance["unrecordedMessages"] = max(0, totals["total"] - provenance["recordedMessages"])
    return {"account": account, "user": user, "source": "local-qq-copy", "dataRevision": row[0],
        "counts": {"messages": totals["total"], "validTexts": totals["valid"], "peerValidTexts": totals["peer"]},
        "range": {"startMs": totals["first"], "endMs": totals["last"]},
        "normalizeVersions": [{"version": str(item[0])[:128], "messages": item[1]} for item in versions[:16]],
        "moreNormalizeVersions": len(versions) > 16, "forward": forward, **work,
        "completeness": "not-proven", "scanBoundary": "interface-window-only", "provenance": provenance}


def read(call):
    try:
        value = call()
        return mapping(value), type(value) is dict
    except Exception:
        # Exception strings can contain paths, identifiers or provider responses.
        return {}, False


def diagnostics(backend, accounts, app_version):
    """Only enumerate explicitly named fields. No logs, SQL, identities or endpoints."""
    health, health_ok = read(backend.health)
    model, model_ok = read(backend.model_source)
    sync_manager = getattr(accounts, "sync_manager", None)
    sync, sync_ok = read(sync_manager.public) if sync_manager is not None else ({}, False)
    imports = getattr(accounts, "imports", None)
    if imports is not None:
        def import_status():
            with imports.lock:
                job = imports.job
                return imports.status(job["public"]["jobId"]) if job else {"state": "idle", "rowsRead": 0}
        imported, import_ok = read(import_status)
    else:
        imported, import_ok = {}, False
    conversations = mapping(sync.get("conversations"))
    histograms = {kind: {} for kind in ("forward", "history", "reconcile")}
    for item in islice(conversations.values(), 500):
        item = mapping(item)
        for kind in histograms:
            payload = item if kind == "forward" else mapping(item.get(kind))
            if payload:
                state = choice(payload.get("state"), WORK_STATES)
                histograms[kind][state] = histograms[kind].get(state, 0) + 1
    version = app_version if isinstance(app_version, str) and len(app_version) <= 32 and re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", app_version) else "unknown"
    compatibility=mapping(sync.get('compatibility'))
    from qq_compatibility import known_version
    return {"schema": "qq-diagnostics-v1", "generatedAt": datetime.now(timezone.utc).isoformat(),
        "software": {"product": "QQVibe", "appVersion": version, "pythonVersion": platform.python_version(),
                     "platform": choice(sys.platform, {"win32", "linux", "darwin"}), "processBits": struct.calcsize("P") * 8},
        'connector':{'qceVersion':known_version(compatibility.get('qceVersion')),
            'qqVersion':known_version(compatibility.get('qqVersion')),'napcatVersion':known_version(compatibility.get('napcatVersion')),
            'adapterVersion':'qce-readonly-http-v1','compatibility':choice(compatibility.get('state'),{'verified-qce-api','unverified-version'})},
        "available": {"health": health_ok, "modelSelection": model_ok, "sync": sync_ok, "import": import_ok},
        "service": {"ok": health.get("ok") is True,
            "dataState": choice(mapping(health.get("data")).get("state"), {"ready", "idle", "error", "account-unavailable"}),
            "modelState": choice(mapping(health.get("model")).get("state"), {"ready", "idle", "loading", "missing", "error"}),
            "modelProvider": choice(mapping(health.get("model")).get("provider"), {"cpu", "webgpu"}),
            "selectedMode": choice(model.get("mode"), {"local", "api"}),
            "protocol": choice(mapping(model.get("api")).get("protocol"),
                               {"anthropic", "responses", "chat_completions", "gemini", "ollama"})},
        "sync": {"state": choice(sync.get("state"), SYNC_STATES), "reason": reason(sync.get("reason")),
            "enabled": sync.get("enabled") is True, "liveValidated": sync.get("liveValidated") is True,
            "requestsTotal": count(sync.get("requestsTotal")), "lastSuccessAtMs": count(sync.get("lastSuccessAtMs")),
            "conversationCount": len(conversations), "sampledConversations": min(len(conversations), 500),
            "states": histograms},
        "import": {"state": choice(imported.get("state"),
            {"idle", "reading", "identity", "ready", "normalizing", "committing", "complete", "cancelled", "failed"}),
            "rowsRead": count(imported.get("rowsRead"))}}

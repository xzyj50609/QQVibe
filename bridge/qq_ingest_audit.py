"""Versioned auxiliary ingest receipts, joined to accepted observations atomically.

No paths, tokens, endpoints or source bodies belong here. Reads never create
tables. A receipt describes one interface window or one parsed export snapshot,
not proof that the remote conversation is complete.
"""
from __future__ import annotations

import re

KINDS = {"forward", "history", "reconcile", "file-import"}
FORMATS = {"qce-api", "qce-single-json", "qce-chunked-jsonl"}
STATES = {"complete", "complete-empty", "partial", "error"}
DISPOSITIONS = {"inserted", "unchanged", "recalled", "revised", "conflicts"}
SOURCE_BITS = {"forward": 1, "history": 2, "reconcile": 4, "file-import": 8}
MAX_INTEGER = 9007199254740991
FIELDS = {"kind", "format", "status", "reason", "windowStartMs", "windowEndMs",
          "sourceSnapshot", "sourceVersion", "rejectedRows"}
DDL = (
    """CREATE TABLE qq_ingest_runs_v1 (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      account_key TEXT NOT NULL,
      conversation_key TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('forward','history','reconcile','file-import')),
      source_format TEXT NOT NULL CHECK (source_format IN ('qce-api','qce-single-json','qce-chunked-jsonl')),
      source_snapshot TEXT,
      source_version TEXT,
      status TEXT NOT NULL CHECK (status IN ('complete','complete-empty','partial','error')),
      reason TEXT,
      completed_at_ms INTEGER NOT NULL CHECK (typeof(completed_at_ms)='integer' AND completed_at_ms>=0),
      window_start_ms INTEGER,
      window_end_ms INTEGER,
      first_record_ms INTEGER,
      last_record_ms INTEGER,
      accepted_rows INTEGER NOT NULL CHECK (accepted_rows>=0),
      rejected_rows INTEGER NOT NULL CHECK (rejected_rows>=0),
      inserted_rows INTEGER NOT NULL CHECK (inserted_rows>=0),
      unchanged_rows INTEGER NOT NULL CHECK (unchanged_rows>=0),
      recalled_rows INTEGER NOT NULL CHECK (recalled_rows>=0),
      revised_rows INTEGER NOT NULL CHECK (revised_rows>=0),
      conflict_rows INTEGER NOT NULL CHECK (conflict_rows>=0),
      data_revision INTEGER NOT NULL CHECK (data_revision>=1),
      CHECK ((window_start_ms IS NULL AND window_end_ms IS NULL) OR
             (window_start_ms IS NOT NULL AND window_end_ms IS NOT NULL AND
              window_start_ms>=0 AND window_end_ms>=window_start_ms)),
      FOREIGN KEY (account_key,conversation_key) REFERENCES conversations(account_key,conversation_key)
    )""",
    """CREATE TABLE qq_ingest_observations_v1 (
      run_id INTEGER NOT NULL,
      message_key TEXT NOT NULL,
      fingerprint TEXT NOT NULL,
      normalize_version TEXT NOT NULL,
      disposition TEXT NOT NULL CHECK (disposition IN ('inserted','unchanged','recalled','revised','conflicts')),
      PRIMARY KEY (run_id,message_key,fingerprint),
      FOREIGN KEY (run_id) REFERENCES qq_ingest_runs_v1(run_id) ON DELETE CASCADE,
      FOREIGN KEY (message_key) REFERENCES messages(message_key) ON DELETE CASCADE
    )""",
    "CREATE INDEX idx_qq_ingest_runs_scope ON qq_ingest_runs_v1(account_key,conversation_key,run_id)",
    "CREATE INDEX idx_qq_ingest_observations_message ON qq_ingest_observations_v1(message_key,run_id)",
    """CREATE TABLE qq_ingest_message_sources_v1 (
      message_key TEXT PRIMARY KEY,
      account_key TEXT NOT NULL,
      conversation_key TEXT NOT NULL,
      source_mask INTEGER NOT NULL CHECK (typeof(source_mask)='integer' AND source_mask BETWEEN 1 AND 15),
      FOREIGN KEY (message_key) REFERENCES messages(message_key) ON DELETE CASCADE,
      FOREIGN KEY (account_key,conversation_key) REFERENCES conversations(account_key,conversation_key)
    )""",
    "CREATE INDEX idx_qq_ingest_message_sources_scope ON qq_ingest_message_sources_v1(account_key,conversation_key,source_mask)",
)


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError("invalid-ingest-audit-integer")
    return value


def validate_receipt(value):
    if type(value) is not dict or set(value) - FIELDS or not {"kind", "format", "status"} <= set(value):
        raise ValueError("invalid-ingest-receipt")
    result = dict(value)
    if any(not isinstance(result[name], str) for name in ("kind", "format", "status")) or result["kind"] not in KINDS or result["format"] not in FORMATS or result["status"] not in STATES:
        raise ValueError("invalid-ingest-receipt")
    if (result["kind"] == "file-import") == (result["format"] == "qce-api"):
        raise ValueError("invalid-ingest-source-format")
    start, end = result.get("windowStartMs"), result.get("windowEndMs")
    if (start is None) != (end is None):
        raise ValueError("invalid-ingest-window")
    if start is not None and integer(start) > integer(end):
        raise ValueError("invalid-ingest-window")
    if result["kind"] != "file-import" and start is None:
        raise ValueError("invalid-ingest-window")
    snapshot = result.get("sourceSnapshot")
    if snapshot is not None and (not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot)):
        raise ValueError("invalid-ingest-source-snapshot")
    version = result.get("sourceVersion")
    if version is not None and (not isinstance(version, str) or len(version) > 128 or
                               not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version)):
        raise ValueError("invalid-ingest-source-version")
    from qq_support import REASONS
    reason = result.get("reason")
    if reason is not None and (not isinstance(reason, str) or reason not in REASONS):
        raise ValueError("invalid-ingest-reason")
    result["rejectedRows"] = integer(result.get("rejectedRows", 0))
    if result["status"] in {"complete", "complete-empty"} and (reason is not None or result["rejectedRows"]):
        raise ValueError("invalid-ingest-completion")
    if result["kind"] == "file-import" and snapshot is None:
        raise ValueError("missing-import-source-snapshot")
    return result


def exists(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE name IN "
        "('qq_ingest_runs_v1','qq_ingest_observations_v1','idx_qq_ingest_runs_scope',"
        "'idx_qq_ingest_observations_message','qq_ingest_message_sources_v1','idx_qq_ingest_message_sources_scope') LIMIT 1").fetchone() is not None


def validate_table(connection):
    def normalized(sql):
        return re.sub(r"\s+", " ", sql or "").strip().rstrip(";").casefold()
    actual = {row[0]: normalized(row[1]) for row in connection.execute("SELECT name,sql FROM sqlite_master")}
    for statement in DDL:
        name = re.match(r"CREATE (?:TABLE|INDEX) (\w+)", statement)[1]
        if actual.get(name) != normalized(statement):
            raise ValueError("invalid-ingest-audit-schema")


def validate_database(connection):
    if not exists(connection):
        return
    validate_table(connection)
    mismatched = connection.execute("SELECT 1 FROM qq_ingest_observations_v1 o "
        "JOIN qq_ingest_runs_v1 r ON r.run_id=o.run_id JOIN messages m ON m.message_key=o.message_key "
        "WHERE r.account_key<>m.account_key OR r.conversation_key<>m.conversation_key LIMIT 1").fetchone()
    if mismatched:
        raise ValueError("ingest-audit-scope-mismatch")
    # The bounded-size projection must exactly equal the source set observed in
    # the full journal. Verify on backup/restore, outside ordinary UI reads.
    projected = connection.execute("SELECT 1 FROM qq_ingest_message_sources_v1 c JOIN messages m ON m.message_key=c.message_key "
        "WHERE c.account_key<>m.account_key OR c.conversation_key<>m.conversation_key LIMIT 1").fetchone()
    if projected:
        raise ValueError("ingest-audit-projection-scope-mismatch")
    expected = ("SELECT o.message_key,MAX(r.kind='forward')+2*MAX(r.kind='history')+4*MAX(r.kind='reconcile')+"
        "8*MAX(r.kind='file-import') AS mask FROM qq_ingest_observations_v1 o "
        "JOIN qq_ingest_runs_v1 r ON r.run_id=o.run_id GROUP BY o.message_key")
    for left, right in (("SELECT message_key,source_mask FROM qq_ingest_message_sources_v1", expected),
                        (expected, "SELECT message_key,source_mask FROM qq_ingest_message_sources_v1")):
        if connection.execute("SELECT 1 FROM (" + left + " EXCEPT " + right + ") LIMIT 1").fetchone():
            raise ValueError("ingest-audit-projection-differs")
    for row in connection.execute("SELECT kind,source_format,status,reason,window_start_ms,window_end_ms,"
                                  "source_snapshot,source_version,rejected_rows FROM qq_ingest_runs_v1"):
        validate_receipt(dict(zip(("kind", "format", "status", "reason", "windowStartMs", "windowEndMs",
                                   "sourceSnapshot", "sourceVersion", "rejectedRows"), row)))


def begin(cursor, account, user, receipt, now_ms):
    integer(now_ms)
    cursor.execute("INSERT INTO qq_ingest_runs_v1(account_key,conversation_key,kind,source_format,source_snapshot,"
        "source_version,status,reason,completed_at_ms,window_start_ms,window_end_ms,accepted_rows,rejected_rows,"
        "inserted_rows,unchanged_rows,recalled_rows,revised_rows,conflict_rows,data_revision) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,0,0,0,0,0,1)",
        (account, user, receipt["kind"], receipt["format"], receipt.get("sourceSnapshot"),
         receipt.get("sourceVersion"), receipt["status"], receipt.get("reason"), now_ms,
         receipt.get("windowStartMs"), receipt.get("windowEndMs"), receipt["rejectedRows"]))
    return cursor.lastrowid


def observe(cursor, run_id, key, fingerprint, normalize_version, disposition, account, user, kind):
    if not isinstance(normalize_version, str) or not 1 <= len(normalize_version) <= 128 or any(ord(c) < 32 for c in normalize_version):
        raise ValueError("invalid-normalize-version")
    cursor.execute("INSERT OR IGNORE INTO qq_ingest_observations_v1 VALUES (?,?,?,?,?)",
                   (run_id, key, fingerprint, normalize_version, disposition))
    cursor.execute("INSERT INTO qq_ingest_message_sources_v1 VALUES (?,?,?,?) ON CONFLICT(message_key) "
                   "DO UPDATE SET source_mask=source_mask | excluded.source_mask WHERE (source_mask & excluded.source_mask)=0",
                   (key, account, user, SOURCE_BITS[kind]))


def finish(cursor, run_id, outcome, seen, bounds, revision, completed_at_ms):
    integer(completed_at_ms)
    status = cursor.execute("SELECT status FROM qq_ingest_runs_v1 WHERE run_id=?", (run_id,)).fetchone()[0]
    if status in {"error", "complete-empty"} and seen:
        raise ValueError("invalid-ingest-empty-state")
    cursor.execute("UPDATE qq_ingest_runs_v1 SET accepted_rows=?,first_record_ms=?,last_record_ms=?,"
        "inserted_rows=?,unchanged_rows=?,recalled_rows=?,revised_rows=?,conflict_rows=?,data_revision=?,completed_at_ms=? WHERE run_id=?",
        (seen, *(bounds or (None, None)), *[outcome[k] for k in ("inserted", "unchanged", "recalled", "revised", "conflicts")],
         revision, completed_at_ms, run_id))


def run_view(row):
    return {"runId": row["run_id"], "kind": row["kind"], "format": row["source_format"],
        "sourceSnapshot": row["source_snapshot"], "sourceVersion": row["source_version"],
        "state": row["status"], "reason": row["reason"], "completedAtMs": row["completed_at_ms"],
        "interfaceWindow": None if row["window_start_ms"] is None else [row["window_start_ms"], row["window_end_ms"]],
        "recordRange": None if row["first_record_ms"] is None else [row["first_record_ms"], row["last_record_ms"]],
        "acceptedRows": row["accepted_rows"], "rejectedRows": row["rejected_rows"],
        "counts": {k: row[column] for k, column in (("inserted", "inserted_rows"), ("unchanged", "unchanged_rows"),
             ("recalled", "recalled_rows"), ("revised", "revised_rows"), ("conflicts", "conflict_rows"))},
        "dataRevision": row["data_revision"]}


def page(connection, account, user, before=None, limit=20, key=None, kind=None):
    integer(limit, 1)
    if limit > 50 or before is not None and integer(before, 1) < 1:
        raise ValueError("invalid-ingest-history-page")
    if kind is not None and kind not in KINDS:
        raise ValueError("invalid-ingest-history-kind")
    if not exists(connection):
        return {"entries": [], "nextBefore": None}
    values = [account, user]
    where = ["r.account_key=?", "r.conversation_key=?"]
    if kind is not None:
        where.append("r.kind=?"); values.append(kind)
    if before is not None:
        where.append("r.run_id<?"); values.append(before)
    join = ""
    if key is not None:
        join = " JOIN qq_ingest_observations_v1 o ON o.run_id=r.run_id"
        where.append("o.message_key=?"); values.append(key)
    if key is not None:
        # Paginate complete run identities, with at most five version samples per
        # run. Competing versions never consume the next run's pagination slot.
        ids = [row[0] for row in connection.execute("SELECT DISTINCT r.run_id FROM qq_ingest_runs_v1 r" + join +
            " WHERE " + " AND ".join(where) + " ORDER BY r.run_id DESC LIMIT ?", (*values, limit + 1))]
        chosen = ids[:limit]
        entries = []
        for run_id in chosen:
            row = connection.execute("SELECT * FROM qq_ingest_runs_v1 WHERE run_id=?", (run_id,)).fetchone()
            observations = connection.execute("SELECT fingerprint,normalize_version,disposition FROM qq_ingest_observations_v1 "
                "WHERE run_id=? AND message_key=? ORDER BY fingerprint LIMIT 5", (run_id, key)).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM qq_ingest_observations_v1 WHERE run_id=? AND message_key=?",
                                       (run_id, key)).fetchone()[0]
            entries.append({**run_view(row), "observationCount": total,
                "observations": [{"fingerprint": item[0], "normalizeVersion": item[1], "disposition": item[2]} for item in observations]})
        return {"entries": entries, "nextBefore": chosen[-1] if len(ids) > limit else None}
    rows = connection.execute("SELECT r.* FROM qq_ingest_runs_v1 r WHERE " + " AND ".join(where) +
        " ORDER BY r.run_id DESC LIMIT ?", (*values, limit + 1)).fetchall()
    return {"entries": [run_view(row) for row in rows[:limit]],
            "nextBefore": rows[limit - 1]["run_id"] if len(rows) > limit else None}


def summary(connection, account, user):
    if not exists(connection):
        return {"recordedMessages": 0, "sources": [], "runCount": 0, "forwardSuccesses": 0, "lastForwardSuccessAtMs": None}
    columns = ",".join("COALESCE(SUM((source_mask & " + str(bit) + ")<>0),0)" for bit in SOURCE_BITS.values())
    projected = connection.execute("SELECT COUNT(*)," + columns + " FROM qq_ingest_message_sources_v1 "
        "WHERE account_key=? AND conversation_key=?", (account, user)).fetchone()
    runs = connection.execute("SELECT COUNT(*),COALESCE(SUM(kind='forward' AND status IN ('complete','complete-empty')),0) "
        "FROM qq_ingest_runs_v1 WHERE account_key=? AND conversation_key=?", (account, user)).fetchone()
    latest = connection.execute("SELECT completed_at_ms FROM qq_ingest_runs_v1 WHERE account_key=? AND conversation_key=? "
        "AND kind='forward' AND status IN ('complete','complete-empty') ORDER BY run_id DESC LIMIT 1", (account, user)).fetchone()
    return {"recordedMessages": projected[0], "sources": [{"kind": kind, "messages": projected[index + 1]}
            for index, kind in enumerate(SOURCE_BITS) if projected[index + 1]],
            "runCount": runs[0], "forwardSuccesses": runs[1], "lastForwardSuccessAtMs": latest[0] if latest else None}


def last_success(connection, account):
    if not exists(connection):
        return None
    row = connection.execute("SELECT completed_at_ms FROM qq_ingest_runs_v1 WHERE account_key=? "
        "AND kind='forward' AND status IN ('complete','complete-empty') ORDER BY run_id DESC LIMIT 1", (account,)).fetchone()
    return row[0] if row else None

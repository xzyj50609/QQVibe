"""Atomic, account-scoped persistence for one inference per message batch.

The legacy per-message results stay in results_v2. This module stores one model
result per batch and a separate coverage index; it never copies that result into
per-message rows or reads WeChat/model data itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing, nullcontext
from functools import wraps
from pathlib import Path

from profile_signals import STYLE_LABELS
from profile_state import empty_state
from analysis_targets import target as matches_target


BATCH_VERSION = "message-batch-v1"
QUOTED_BACKFILL_VERSION = "quoted-reply-v1"
SHARD = re.compile(r"message__message_\d+\.db\Z")
QQ_SCOPE = re.compile(r"qq:(?:u:[^\s\x00-\x1f\x7f]{1,256}|g:[1-9][0-9]{0,255})\Z")
AXES = ("EI", "SN", "TF", "JP")


def guarded_transaction(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.transaction_guard():
            return method(self, *args, **kwargs)
    return guarded


def _position(value, session=None):
    if (not isinstance(value, (list, tuple)) or len(value) != 3 or
            type(value[0]) is not int or type(value[2]) is not int or
            value[0] < 0 or value[2] < 0 or
            not isinstance(value[1], str) or
            not (SHARD.fullmatch(value[1]) or QQ_SCOPE.fullmatch(value[1]))):
        raise ValueError("invalid batch position")
    if value[1].startswith("qq:") and session is not None and value[1] != "qq:" + session:
        raise ValueError("batch position belongs to another conversation")
    return tuple(value)


def _scope(account, session, version, subject):
    if not all(isinstance(value, str) and value for value in (account, session, version)):
        raise ValueError("invalid batch scope")
    if not isinstance(subject, str):
        raise ValueError("invalid batch subject")
    return account, session, version, subject, BATCH_VERSION


def _state(value):
    result = json.loads(json.dumps(value if value is not None else empty_state(), ensure_ascii=False))
    if not isinstance(result, dict) or not set(empty_state()) <= set(result):
        raise ValueError("incompatible profile state")
    result.setdefault("batchCount", 0)
    return result


def _context(value):
    if not isinstance(value, list) or len(value) > 3 or any(not isinstance(item, dict) for item in value):
        raise ValueError("batch context must contain at most three messages")
    return json.loads(json.dumps(value, ensure_ascii=False))


def _scores(result, field):
    values = result.get(field) or []
    if not isinstance(values, list):
        raise ValueError("invalid batch scores")
    for entry in values:
        probability = entry.get("probability") if isinstance(entry, dict) else None
        if (not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or
                not entry["label"] or type(probability) not in (int, float) or
                not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("invalid batch score entry")
    return values


def _segments(records, cursor, char_offset, session=None):
    if not isinstance(records, list) or not 1 <= len(records) <= 256:
        raise ValueError("invalid batch coverage")
    if type(char_offset) is not int or char_offset < 0:
        raise ValueError("invalid batch character offset")
    seen, completed, previous = set(), [], None
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("invalid batch segment")
        stable_id, sender, side = item.get("id"), item.get("senderId"), item.get("side")
        position = _position(item.get("position"), session)
        start, end, length = (item.get(name) for name in ("startOffset", "endOffset", "textLength"))
        if (not isinstance(stable_id, str) or not stable_id or stable_id in seen or
                not isinstance(sender, str) or side not in ("self", "other") or
                any(type(value) is not int for value in (start, end, length)) or
                not 0 <= start < end <= length or type(item.get("complete")) is not bool or
                ("target" in item and type(item["target"]) is not bool) or
                (item["complete"] != (end == length)) or
                (previous is not None and position < previous)):
            raise ValueError("invalid batch segment")
        seen.add(stable_id)
        previous = position
        if item["complete"]:
            completed.append(item)
    if previous != cursor:
        raise ValueError("batch cursor must be the last consumed message")
    last = records[-1]
    if char_offset != (0 if last["complete"] else last["endOffset"]):
        raise ValueError("batch character offset does not match last segment")
    return completed


def _merge(state, result, targets, latest, words, is_group, subject,
           tail_scores=(), tail_emotions=()):
    if not targets:
        return
    state["count"] += targets
    state["targetCount"] += targets
    if state["latest"] is None or latest > tuple(state["latest"]):
        state["latest"] = list(latest)
    for field in ("emotion", "intent"):
        for entry in _scores(result, field):
            label = entry["label"]
            state[field][label] = state[field].get(label, 0.0) + targets * entry["probability"]
    for entry in _scores(result, "intentBroad"):
        label = entry["label"]
        state["broad"][label] = state["broad"].get(label, 0.0) + targets * entry["probability"]
    for word, count in words.items():
        state["words"][word] = state["words"].get(word, 0) + count
    score = result.get("score")
    if score is not None:
        if type(score) not in (int, float) or not math.isfinite(score) or not -1 <= score <= 1:
            raise ValueError("invalid batch relationship score")
        rank = state["scoreCount"] - len(tail_scores)
        state["scoreSum"] += targets * score
        state["scoreWeighted"] += (targets * rank + targets * (targets - 1) / 2) * score
        state["scoreWeighted"] += sum(tail_scores) * targets
        state["scoreCount"] += targets
    emotion = _scores(result, "emotion")
    if emotion:
        rank = state["moodCount"] - len(tail_emotions)
        rank_sum = targets * rank + targets * (targets - 1) / 2
        for tail in tail_emotions:
            for entry in tail:
                raw = entry.get("rawLabel") or entry["label"]
                state["mood"][raw]["weighted"] += targets * entry["probability"]
        for entry in emotion:
            raw = entry.get("rawLabel") or entry["label"]
            total = state["mood"].setdefault(raw, {"label": entry["label"], "sum": 0.0, "weighted": 0.0})
            total["sum"] += targets * entry["probability"]
            total["weighted"] += rank_sum * entry["probability"]
            total["label"] = entry["label"]
        state["moodCount"] += targets
    style = result.get("styleEvidence")
    if style is not None:
        if (not isinstance(style, dict) or set(style) != set(STYLE_LABELS) or
                any(type(value) not in (int, float) or not math.isfinite(value) or
                    not 0 <= value <= 1 for value in style.values())):
            raise ValueError("invalid batch style evidence")
        state["styleCount"] += targets
        for key in STYLE_LABELS:
            state["style"][key] += targets * style[key]
    # A whole group can mix senders for mood/intent, but has no group MBTI.
    if is_group and not subject:
        return
    evidence = result.get("personalityEvidence")
    if evidence is not None and (not isinstance(evidence, dict) or set(evidence) != set(AXES)):
        raise ValueError("invalid batch personality evidence")
    supported = False
    for axis in AXES:
        left, right = axis
        distribution = evidence.get(axis) if evidence else None
        if distribution is not None and (
                not isinstance(distribution, dict) or set(distribution) != {left, right, "insufficient"} or
                any(type(value) not in (int, float) or not math.isfinite(value) or
                    not 0 <= value <= 1 for value in distribution.values())):
            raise ValueError("invalid batch personality distribution")
        totals = state["axes"][axis]
        if distribution is None or distribution["insufficient"] >= max(distribution[left], distribution[right]):
            totals[3] += targets
        else:
            totals[0] += targets * distribution[left]
            totals[1] += targets * distribution[right]
            totals[2] += targets
            supported = True
    state["supported"] += targets if supported else 0


def _combined_result(fragments):
    """Length-weight prior partial judgments into one completed message signal."""
    # The protocol permits an empty result for whitespace-only target fragments.
    # They add coverage, not emotional evidence; later nonblank fragments still count.
    fragments = [(length, result) for length, result in fragments if result is not None]
    if not fragments:
        return None
    total = sum(length for length, _result in fragments)
    if total <= 0:
        return None
    combined = {}
    for field in ("emotion", "intent", "intentBroad"):
        totals = {}
        for length, result in fragments:
            for entry in _scores(result, field):
                raw = entry.get("rawLabel") or entry["label"]
                previous = totals.setdefault(raw, {"label": entry["label"], "rawLabel": raw, "mass": 0.0})
                previous["label"] = entry["label"]
                previous["mass"] += length * entry["probability"]
        combined[field] = [{"label": value["label"], "rawLabel": value["rawLabel"],
                            "probability": value["mass"] / total} for value in totals.values()]
    scores = [result.get("score") for _length, result in fragments]
    combined["score"] = (sum(length * score for (length, _result), score in zip(fragments, scores)) / total
                         if all(type(score) in (int, float) and math.isfinite(score) for score in scores) else None)
    styles = [result.get("styleEvidence") for _length, result in fragments]
    combined["styleEvidence"] = ({key: sum(length * style[key] for (length, _result), style in
                                           zip(fragments, styles)) / total for key in STYLE_LABELS}
                                 if all(isinstance(style, dict) and set(style) == set(STYLE_LABELS)
                                        for style in styles) else None)
    personalities = [result.get("personalityEvidence") for _length, result in fragments]
    combined["personalityEvidence"] = ({axis: {choice: sum(
        length * evidence[axis][choice] for (length, _result), evidence in zip(fragments, personalities)) / total
        for choice in (*axis, "insufficient")} for axis in AXES}
        if all(isinstance(evidence, dict) and set(evidence) == set(AXES) for evidence in personalities)
        else None)
    return combined


def _tail_evidence(conn, scope, legacy_max, subject, minimum):
    """Read older-version and batch contributions after the first new position once."""
    account, session, base_version, _subject, _batch_version = scope
    legacy_where = ("account=? AND session=? AND version=? AND rowid<=? AND side='other' "
                    "AND (sort_seq,shard,local_id)>(?,?,?)")
    args = (account, session, base_version, legacy_max, *minimum)
    if subject:
        legacy_where += " AND sender=?"
        args += (subject,)
    tails = []
    for seq, shard, local, score, raw in conn.execute(
            "SELECT sort_seq,shard,local_id,score,result FROM results_v2 WHERE " + legacy_where, args):
        tails.append(((seq, shard, local), score, json.loads(raw).get("emotion") or []))
    for seq, shard, local, score, raw in conn.execute(
            "SELECT sort_seq,shard,local_id,score,mood_json FROM batch_coverage_v1 "
            "WHERE account=? AND session=? AND base_version=? AND subject=? AND batch_version=? "
            "AND counted=1 AND (sort_seq,shard,local_id)>(?,?,?)", (*scope, *minimum)):
        tails.append(((seq, shard, local), score, json.loads(raw) if raw else []))
    return tails


class BatchStateStore:
    def __init__(self, result_store):
        self.path = Path(result_store.path)
        self.transaction_guard = getattr(result_store, "transaction_guard", nullcontext)
        if not self.path.is_file():
            raise ValueError("batch state requires an existing account result store")
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS batch_progress_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,base_version TEXT NOT NULL,"
                         "subject TEXT NOT NULL,batch_version TEXT NOT NULL,cursor_seq INTEGER,"
                         "cursor_shard TEXT,cursor_local INTEGER,char_offset INTEGER NOT NULL,"
                         "context_json TEXT NOT NULL,state_json TEXT NOT NULL,"
                         "complete INTEGER NOT NULL DEFAULT 0,legacy_max_rowid INTEGER NOT NULL DEFAULT 0,"
                         "PRIMARY KEY(account,session,base_version,subject,batch_version))")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(batch_progress_v1)")}
            if "complete" not in columns:
                conn.execute("ALTER TABLE batch_progress_v1 ADD COLUMN complete INTEGER NOT NULL DEFAULT 0")
            if "legacy_max_rowid" not in columns:
                conn.execute("ALTER TABLE batch_progress_v1 ADD COLUMN legacy_max_rowid INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE TABLE IF NOT EXISTS batch_runs_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,base_version TEXT NOT NULL,"
                         "subject TEXT NOT NULL,batch_version TEXT NOT NULL,batch_id TEXT NOT NULL,"
                         "anchor_id TEXT,anchor_seq INTEGER,anchor_shard TEXT,anchor_local INTEGER,"
                         "fingerprint TEXT NOT NULL,consumed_json TEXT NOT NULL,result_json TEXT NOT NULL,"
                         "target_count INTEGER NOT NULL,new_target_count INTEGER NOT NULL,"
                         "PRIMARY KEY(account,session,base_version,subject,batch_version,batch_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS batch_coverage_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,base_version TEXT NOT NULL,"
                         "subject TEXT NOT NULL,batch_version TEXT NOT NULL,message_id TEXT NOT NULL,"
                         "batch_id TEXT NOT NULL,counted INTEGER NOT NULL,sort_seq INTEGER NOT NULL,"
                         "shard TEXT NOT NULL,local_id INTEGER NOT NULL,score REAL,mood_json TEXT,"
                         "PRIMARY KEY(account,session,base_version,subject,batch_version,message_id))")
            coverage_columns = {row[1] for row in conn.execute("PRAGMA table_info(batch_coverage_v1)")}
            if "score" not in coverage_columns:
                conn.execute("ALTER TABLE batch_coverage_v1 ADD COLUMN score REAL")
            if "mood_json" not in coverage_columns:
                conn.execute("ALTER TABLE batch_coverage_v1 ADD COLUMN mood_json TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS batch_coverage_recent_v1 ON batch_coverage_v1 "
                         "(account,session,base_version,subject,batch_version,sort_seq DESC,shard DESC,local_id DESC)")
            conn.execute("CREATE TABLE IF NOT EXISTS batch_fragments_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,base_version TEXT NOT NULL,"
                         "subject TEXT NOT NULL,batch_version TEXT NOT NULL,message_id TEXT NOT NULL,"
                         "batch_id TEXT NOT NULL,start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,"
                         "text_length INTEGER NOT NULL,PRIMARY KEY(account,session,base_version,subject,batch_version,message_id,start_offset))")
            conn.execute("CREATE TABLE IF NOT EXISTS quoted_backfill_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,base_version TEXT NOT NULL,"
                         "subject TEXT NOT NULL,backfill_version TEXT NOT NULL,"
                         "ceiling_seq INTEGER NOT NULL,ceiling_shard TEXT NOT NULL,ceiling_local INTEGER NOT NULL,"
                         "cursor_seq INTEGER,cursor_shard TEXT,cursor_local INTEGER,complete INTEGER NOT NULL DEFAULT 0,"
                         "PRIMARY KEY(account,session,base_version,subject,backfill_version))")

    def load(self, account, session, base_version, subject):
        scope = _scope(account, session, base_version, subject)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            row = conn.execute("SELECT cursor_seq,cursor_shard,cursor_local,char_offset,context_json,state_json,complete "
                               "FROM batch_progress_v1 WHERE account=? AND session=? AND base_version=? "
                               "AND subject=? AND batch_version=?", scope).fetchone()
        if row is None:
            return None
        seq, shard, local, offset, context, state, complete = row
        return {"cursor": (seq, shard, local) if seq is not None else None,
                "charOffset": offset, "context": json.loads(context), "state": json.loads(state),
                "complete": bool(complete)}

    def legacy_known(self, account, session, base_version, subject, ids):
        """Read only results inside the immutable legacy boundary captured by seed()."""
        if not ids:
            return set()
        scope = _scope(account, session, base_version, subject)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn:
            boundary = conn.execute(
                "SELECT legacy_max_rowid FROM batch_progress_v1 WHERE account=? AND session=? "
                "AND base_version=? AND subject=? AND batch_version=?", scope).fetchone()
            if boundary is None:
                raise RuntimeError("batch scope must be seeded before known lookup")
            return {row[0] for row in conn.execute(
                "SELECT id FROM results_v2 WHERE account=? AND session=? AND version=? "
                "AND rowid<=? AND id IN (" + ",".join("?" for _ in ids) + ")",
                (account, session, base_version, boundary[0], *ids))}

    def move_cursor(self, account, session, base_version, subject, cursor, context, *, char_offset=0):
        """Persist a service-approved scan position without manufacturing inference results."""
        if cursor is None:
            return
        cursor = _position(cursor, session)
        scope = _scope(account, session, base_version, subject)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute("UPDATE batch_progress_v1 SET cursor_seq=?,cursor_shard=?,cursor_local=?,"
                         "char_offset=?,context_json=?,complete=0 WHERE account=? AND session=? "
                         "AND base_version=? AND subject=? AND batch_version=?",
                         (*cursor, char_offset, json.dumps(context, ensure_ascii=False), *scope))

    def quoted_backfill_pending(self, account, session, base_version, subject, cursor):
        if cursor is None:
            return False
        scope = (*_scope(account, session, base_version, subject)[:4], QUOTED_BACKFILL_VERSION)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn:
            row = conn.execute("SELECT complete FROM quoted_backfill_v1 WHERE account=? AND session=? "
                               "AND base_version=? AND subject=? AND backfill_version=?", scope).fetchone()
        return row is None or not bool(row[0])

    def begin_quoted_backfill(self, account, session, base_version, subject, ceiling):
        boundary = _position(ceiling, session)
        scope = (*_scope(account, session, base_version, subject)[:4], QUOTED_BACKFILL_VERSION)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO quoted_backfill_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                         (*scope, *boundary, None, None, None))
            row = conn.execute("SELECT ceiling_seq,ceiling_shard,ceiling_local,"
                               "cursor_seq,cursor_shard,cursor_local,complete FROM quoted_backfill_v1 "
                               "WHERE account=? AND session=? AND base_version=? AND subject=? "
                               "AND backfill_version=?", scope).fetchone()
        return {"ceiling": tuple(row[:3]), "cursor": tuple(row[3:6]) if row[3] is not None else None,
                "complete": bool(row[6])}

    def advance_quoted_backfill(self, account, session, base_version, subject, position=None, *, complete=False):
        next_position = _position(position, session) if position is not None else None
        scope = (*_scope(account, session, base_version, subject)[:4], QUOTED_BACKFILL_VERSION)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT ceiling_seq,ceiling_shard,ceiling_local,"
                               "cursor_seq,cursor_shard,cursor_local,complete FROM quoted_backfill_v1 "
                               "WHERE account=? AND session=? AND base_version=? AND subject=? "
                               "AND backfill_version=?", scope).fetchone()
            if row is None:
                raise ValueError("quoted backfill must be started before advancing")
            if row[6]:
                return
            ceiling = tuple(row[:3])
            previous = tuple(row[3:6]) if row[3] is not None else None
            if next_position is not None and (next_position > ceiling or
                                              previous is not None and next_position < previous):
                raise ValueError("quoted backfill cursor is outside its frozen prefix")
            final = next_position or previous
            conn.execute("UPDATE quoted_backfill_v1 SET cursor_seq=?,cursor_shard=?,cursor_local=?,"
                         "complete=? WHERE account=? AND session=? AND base_version=? AND subject=? "
                         "AND backfill_version=?", (*(final or (None, None, None)), int(complete), *scope))

    def mark_complete(self, account, session, base_version, subject):
        scope = _scope(account, session, base_version, subject)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE batch_progress_v1 SET complete=1 WHERE account=? "
                                   "AND session=? AND base_version=? AND subject=? AND batch_version=?",
                                   scope).rowcount
            if not changed:
                raise ValueError("batch scope must be seeded before completion")
        return self.load(account, session, base_version, subject)

    def offsets(self, account, session, base_version, subject, ids):
        """Highest stored Unicode-codepoint end for each still-incomplete message."""
        scope = _scope(account, session, base_version, subject)
        if not isinstance(ids, (list, tuple)) or len(ids) > 500 or any(
                not isinstance(item, str) or not item for item in ids):
            raise ValueError("invalid offset ids")
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn:
            rows = conn.execute(
                f"SELECT f.message_id,MAX(f.end_offset) FROM batch_fragments_v1 f "
                f"LEFT JOIN batch_coverage_v1 c ON c.account=f.account AND c.session=f.session "
                f"AND c.base_version=f.base_version AND c.subject=f.subject "
                f"AND c.batch_version=f.batch_version AND c.message_id=f.message_id "
                f"WHERE f.account=? AND f.session=? AND f.base_version=? AND f.subject=? "
                f"AND f.batch_version=? AND f.message_id IN ({marks}) AND c.message_id IS NULL "
                f"GROUP BY f.message_id", (*scope, *ids)).fetchall()
        return dict(rows)

    def outcomes(self, account, session, base_version, subject, ids=None, limit=80):
        """Return coverage markers; only the anchor carries the one stored batch result."""
        scope = _scope(account, session, base_version, subject)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid outcome limit")
        if ids is not None and (not isinstance(ids, (list, tuple)) or len(ids) > 500 or
                                any(not isinstance(item, str) or not item for item in ids)):
            raise ValueError("invalid outcome ids")
        sql = ("SELECT c.message_id,c.batch_id,c.counted,r.anchor_id,r.anchor_seq,r.anchor_shard,"
               "r.anchor_local,r.target_count,r.result_json FROM batch_coverage_v1 c "
               "JOIN batch_runs_v1 r ON r.account=c.account AND r.session=c.session "
               "AND r.base_version=c.base_version AND r.subject=c.subject "
               "AND r.batch_version=c.batch_version AND r.batch_id=c.batch_id "
               "WHERE c.account=? AND c.session=? AND c.base_version=? AND c.subject=? AND c.batch_version=?")
        args = list(scope)
        if ids is not None:
            if not ids:
                return {}
            sql += " AND c.message_id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        sql += " ORDER BY c.sort_seq DESC,c.shard DESC,c.local_id DESC LIMIT ?"
        args.append(limit)
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            rows = conn.execute(sql, args).fetchall()
        output = {}
        for stable_id, batch_id, counted, anchor_id, seq, shard, local, target_count, raw in rows:
            is_anchor = stable_id == anchor_id and counted and raw != "null"
            output[stable_id] = {"state": "done" if is_anchor else "batch-covered",
                                 "batchId": batch_id, "anchorId": anchor_id,
                                 "anchorCursor": [seq, shard, local] if seq is not None else None,
                                 "batch": {"messageCount": target_count, "unit": "complete-target-messages"}}
            if is_anchor:
                output[stable_id]["result"] = json.loads(raw)
        return output

    def seed(self, account, session, base_version, subject, state, cursor=None, context=None):
        scope = _scope(account, session, base_version, subject)
        position = _position(cursor, session) if cursor is not None else None
        snapshot, previous = _state(state), _context(context or [])
        with self.transaction_guard(), closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            legacy_where = "account=? AND session=? AND version=?"
            legacy_args = (account, session, base_version)
            # A single-chat subject is the conversation key, which need not be a
            # native sender ID (QQ UID/UIN). Only an explicit member narrows it.
            if subject and subject != session:
                legacy_where += " AND sender=?"
                legacy_args += (subject,)
            legacy_max = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM results_v2 WHERE " + legacy_where,
                                      legacy_args).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO batch_progress_v1 (account,session,base_version,subject,"
                         "batch_version,cursor_seq,cursor_shard,cursor_local,char_offset,context_json,state_json,"
                         "legacy_max_rowid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (*scope, *(position or (None, None, None)), 0,
                          json.dumps(previous, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False),
                          legacy_max))
        return self.load(account, session, base_version, subject)

    @guarded_transaction
    def commit(self, account, session, base_version, subject, *, batch_id, consumed,
               cursor, char_offset, context, result, anchor=None, word_counts=None,
               is_group=False, advance=True):
        scope = _scope(account, session, base_version, subject)
        if not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 128:
            raise ValueError("invalid batch id")
        if not isinstance(consumed, list) or not consumed or type(advance) is not bool:
            raise ValueError("invalid batch coverage or advance mode")
        position = _position(cursor, session) if cursor is not None else None
        batch_end = _position(consumed[-1].get("position"), session)
        segment_offset = 0 if consumed[-1].get("complete") else consumed[-1].get("endOffset")
        completed = _segments(consumed, batch_end, segment_offset, session)
        if advance and (position != batch_end or char_offset != segment_offset):
            raise ValueError("advancing cursor must match last segment")
        if type(char_offset) is not int or char_offset < 0:
            raise ValueError("invalid batch character offset")
        context = _context(context)
        if result is not None and not isinstance(result, dict) or type(is_group) is not bool:
            raise ValueError("invalid batch result")
        if not is_group and not subject:
            raise ValueError("personal portrait requires a subject")
        for item in consumed:
            targeted = item.get("target", item["side"] == "other")
            if targeted and not matches_target(item,subject,is_group=is_group):
                raise ValueError("batch target belongs to another portrait subject")
        targets = [item for item in completed if item.get("target", item["side"] == "other")]
        requested_anchor = _position(anchor, session) if anchor is not None else None
        words = word_counts or {}
        if (not isinstance(words, dict) or
                any(message_id not in {item["id"] for item in targets} or not isinstance(counts, dict) or
                    any(not isinstance(word, str) or not word or type(count) is not int or count < 0
                        for word, count in counts.items()) for message_id, counts in words.items()) or
                (not completed and words)):
            raise ValueError("invalid batch word counts")
        payload = {"anchor": requested_anchor,
                   "consumed": consumed, "cursor": position, "advance": advance,
                   "charOffset": char_offset, "context": context, "result": result,
                   "words": words, "isGroup": is_group}
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT fingerprint FROM batch_runs_v1 WHERE account=? AND session=? "
                               "AND base_version=? AND subject=? AND batch_version=? AND batch_id=?",
                               (*scope, batch_id)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise ValueError("batch id reused with different contents")
                conn.rollback()
                return {"applied": False, **self.load(account, session, base_version, subject)}
            saved = conn.execute("SELECT cursor_seq,cursor_shard,cursor_local,char_offset,state_json,legacy_max_rowid "
                                 "FROM batch_progress_v1 WHERE account=? AND session=? AND base_version=? "
                                 "AND subject=? AND batch_version=?", scope).fetchone()
            if saved is None:
                raise ValueError("batch scope must be seeded before commit")
            old_position = tuple(saved[:3]) if saved[0] is not None else None
            old_offset = saved[3]
            if advance:
                if old_position is not None:
                    if position < old_position or (position == old_position and
                       (old_offset == 0 or not (char_offset > old_offset or
                        char_offset == 0 and consumed[-1]["complete"]))):
                        raise ValueError("batch cursor did not advance")
                    if old_offset and (tuple(consumed[0]["position"]) != old_position or
                                       consumed[0]["startOffset"] != old_offset or
                                       len(consumed) > 1 and not consumed[0]["complete"]):
                        raise ValueError("unfinished message segment was skipped")
                    if not old_offset and tuple(consumed[0]["position"]) <= old_position:
                        raise ValueError("batch reread a complete cursor message")
                elif consumed[0]["startOffset"] != 0:
                    raise ValueError("first batch must start at character zero")
            elif position != old_position or char_offset != old_offset:
                raise ValueError("recent batch cannot move historical cursor")
            state = _state(json.loads(saved[4]))
            legacy_max = saved[5]
            ids = [item["id"] for item in completed]
            existing = set()
            legacy = set()
            if ids:
                marks = ",".join("?" for _ in ids)
                existing = {row[0] for row in conn.execute(
                    f"SELECT message_id FROM batch_coverage_v1 WHERE account=? AND session=? "
                    f"AND base_version=? AND subject=? AND batch_version=? AND message_id IN ({marks})",
                    (*scope, *ids))}
                legacy = {row[0] for row in conn.execute(
                    f"SELECT id FROM results_v2 WHERE account=? AND session=? AND version=? "
                    f"AND rowid<=? AND id IN ({marks})",
                    (account, session, base_version, legacy_max, *ids))}
            conn.execute("INSERT INTO batch_runs_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (*scope, batch_id, None, None, None, None, fingerprint,
                          json.dumps(consumed, ensure_ascii=False), json.dumps(result, ensure_ascii=False),
                          0, 0))
            for item in consumed:
                stable_id = item["id"]
                if stable_id in existing:
                    continue
                prior = conn.execute("SELECT MAX(end_offset),MAX(text_length) FROM batch_fragments_v1 "
                                     "WHERE account=? AND session=? AND base_version=? AND subject=? "
                                     "AND batch_version=? AND message_id=?", (*scope, stable_id)).fetchone()
                expected_start = prior[0] if prior[0] is not None else 0
                if item["startOffset"] != expected_start or (prior[1] is not None and
                                                            item["textLength"] != prior[1]):
                    raise ValueError("non-contiguous message segments")
                conn.execute("INSERT INTO batch_fragments_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (*scope, stable_id, batch_id, item["startOffset"],
                              item["endOffset"], item["textLength"]))
            state["batchCount"] += int(result is not None)
            candidates = [item for item in targets if item["id"] not in existing and item["id"] not in legacy]
            tails = (_tail_evidence(conn, scope, legacy_max, subject,
                                    _position(candidates[0]["position"])) if candidates else [])
            # Full, new targets share one inference. They form one recency block only
            # when no previously counted score/mood signal falls between them.
            fast_batch = bool(candidates) and result is not None and len(candidates) == len(targets) and all(
                item["complete"] and item["startOffset"] == 0
                for item in consumed if item.get("target", item["side"] == "other"))
            if fast_batch:
                first_position = _position(candidates[0]["position"])
                last_position = _position(candidates[-1]["position"])
                fast_batch = not any(first_position < tail_position < last_position and
                                     (score is not None or emotion)
                                     for tail_position, score, emotion in tails)
            fast_result = _combined_result([(1, result)]) if fast_batch else None
            new_targets = 0
            anchor_item = None
            for item in completed:
                stable_id = item["id"]
                if stable_id in existing:
                    continue
                counted = False
                effective = None
                if stable_id not in legacy and item.get("target", item["side"] == "other"):
                    if fast_batch:
                        effective = fast_result
                    else:
                        fragments = [(end - start, json.loads(raw)) for start, end, raw in conn.execute(
                            "SELECT f.start_offset,f.end_offset,r.result_json FROM batch_fragments_v1 f "
                            "JOIN batch_runs_v1 r ON r.account=f.account AND r.session=f.session "
                            "AND r.base_version=f.base_version AND r.subject=f.subject "
                            "AND r.batch_version=f.batch_version AND r.batch_id=f.batch_id "
                            "WHERE f.account=? AND f.session=? AND f.base_version=? AND f.subject=? "
                            "AND f.batch_version=? AND f.message_id=? ORDER BY f.start_offset",
                            (*scope, stable_id))]
                        effective = _combined_result(fragments)
                    if effective is not None:
                        latest = (*item["position"], stable_id)
                        if not fast_batch:
                            target_position = _position(item["position"])
                            tail_scores = [score for tail_position, score, _emotion in tails
                                           if tail_position > target_position and score is not None]
                            tail_emotions = [emotion for tail_position, _score, emotion in tails
                                             if tail_position > target_position and emotion]
                            _merge(state, effective, 1, latest, words.get(stable_id, {}), is_group, subject,
                                   tail_scores, tail_emotions)
                        counted = True
                        new_targets += 1
                        anchor_item = item
                elif stable_id not in legacy and is_group and not subject and item["side"] == "self":
                    state["count"] += 1
                    counted = True
                conn.execute("INSERT INTO batch_coverage_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (*scope, stable_id, batch_id, int(counted), *item["position"],
                              effective.get("score") if counted and effective else None,
                              json.dumps(effective.get("emotion") or [], ensure_ascii=False)
                              if counted and effective else None))
            if fast_batch and fast_result is not None:
                batch_words = {}
                for item in candidates:
                    for word, count in words.get(item["id"], {}).items():
                        batch_words[word] = batch_words.get(word, 0) + count
                tail_scores = [score for tail_position, score, _emotion in tails
                               if tail_position > last_position and score is not None]
                tail_emotions = [emotion for tail_position, _score, emotion in tails
                                 if tail_position > last_position and emotion]
                _merge(state, fast_result, len(candidates),
                       (*candidates[-1]["position"], candidates[-1]["id"]), batch_words,
                       is_group, subject, tail_scores, tail_emotions)
            anchor_position = _position(anchor_item["position"]) if anchor_item else None
            if requested_anchor is not None and requested_anchor != anchor_position:
                raise ValueError("batch anchor must be the last newly analyzed target")
            conn.execute("UPDATE batch_runs_v1 SET anchor_id=?,anchor_seq=?,anchor_shard=?,"
                         "anchor_local=?,target_count=?,new_target_count=? WHERE account=? AND session=? "
                         "AND base_version=? AND subject=? AND batch_version=? AND batch_id=?",
                         (anchor_item["id"] if anchor_item else None,
                          *(anchor_position or (None, None, None)), new_targets, new_targets,
                          *scope, batch_id))
            if advance:
                conn.execute("UPDATE batch_progress_v1 SET cursor_seq=?,cursor_shard=?,cursor_local=?,"
                             "char_offset=?,context_json=?,state_json=? WHERE account=? AND session=? "
                             "AND base_version=? AND subject=? AND batch_version=?",
                             (*position, char_offset, json.dumps(context, ensure_ascii=False),
                              json.dumps(state, ensure_ascii=False), *scope))
            else:
                conn.execute("UPDATE batch_progress_v1 SET state_json=? WHERE account=? AND session=? "
                             "AND base_version=? AND subject=? AND batch_version=?",
                             (json.dumps(state, ensure_ascii=False), *scope))
            conn.commit()
            return {"applied": True, "cursor": position, "charOffset": char_offset,
                    "context": context, "state": state, "newTargets": new_targets,
                    "completeTargets": new_targets, "submittedTargets": len(targets)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

"""Source-scoped API portrait evidence, independent of model IO and raw chat storage.

Construct this adapter before entering a checkpoint write transaction. ``begin``,
``persist`` and ``clear`` use the caller's connection and never commit it. A completed
message stores its effective classification and keywords, not its original text.
The established resume statistics still own unfinished target text.
"""
from __future__ import annotations

from collections import namedtuple
from copy import deepcopy
import hashlib
import json
import math

from api_portrait_statistics import (
    MAX_PENDING_CHARS, _count, _position, _signal, _text, empty_statistics,
    valid_statistics, validate_batch_signal,
)
from batch_state import _combined_result, _merge
from profile_signals import keyword_counts


LEDGER_VERSION = 1
PortraitLedgerScope = namedtuple("PortraitLedgerScope", "account user source_id subject classifier_version")
_COLUMNS = ("account", "session", "source_id", "subject", "classifier_version")
_WHERE = " AND ".join(column + "=?" for column in _COLUMNS)
_TABLES = ("api_portrait_ledger_rows_v1", "api_portrait_ledger_fragments_v1", "api_portrait_ledger_meta_v1")
_FACTS = ("messageId", "position", "speaker", "sender", "target", "group", "localSubject")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS api_portrait_ledger_meta_v1 ("
    "account TEXT NOT NULL,session TEXT NOT NULL,source_id TEXT NOT NULL,subject TEXT NOT NULL,"
    "classifier_version TEXT NOT NULL,version INTEGER NOT NULL,ready INTEGER NOT NULL DEFAULT 0,"
    "generation INTEGER NOT NULL DEFAULT 0,batch_count INTEGER NOT NULL DEFAULT 0,"
    "revision TEXT,prefix_signature TEXT,highwater_json TEXT,state_hash TEXT,"
    "PRIMARY KEY(account,session,source_id,subject,classifier_version))",
    "CREATE TABLE IF NOT EXISTS api_portrait_ledger_rows_v1 ("
    "account TEXT NOT NULL,session TEXT NOT NULL,source_id TEXT NOT NULL,subject TEXT NOT NULL,"
    "classifier_version TEXT NOT NULL,message_id TEXT NOT NULL,sort_seq INTEGER NOT NULL,"
    "shard TEXT NOT NULL,local_id INTEGER NOT NULL,record_json TEXT NOT NULL,"
    "PRIMARY KEY(account,session,source_id,subject,classifier_version,message_id))",
    "CREATE INDEX IF NOT EXISTS api_portrait_ledger_order_v1 ON api_portrait_ledger_rows_v1 "
    "(account,session,source_id,subject,classifier_version,sort_seq,shard,local_id,message_id)",
    "CREATE TABLE IF NOT EXISTS api_portrait_ledger_fragments_v1 ("
    "account TEXT NOT NULL,session TEXT NOT NULL,source_id TEXT NOT NULL,subject TEXT NOT NULL,"
    "classifier_version TEXT NOT NULL,message_id TEXT NOT NULL,fragment_json TEXT NOT NULL,"
    "PRIMARY KEY(account,session,source_id,subject,classifier_version,message_id))",
)


def scope(account, user, source_id, subject, classifier_version):
    values = (account, user, source_id, subject, classifier_version)
    if any(not isinstance(value, str) or len(value) > 512 or "\x00" in value for value in values):
        raise ValueError("invalid API portrait ledger scope")
    if any(not value for value in (account, user, source_id, classifier_version)):
        raise ValueError("invalid API portrait ledger scope")
    return PortraitLedgerScope(*values)


def _scope(value):
    if not isinstance(value, (list, tuple)) or len(value) != 5:
        raise ValueError("invalid API portrait ledger scope")
    return scope(*value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _equivalent(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_equivalent(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equivalent(first, second) for first, second in zip(left, right))
    if type(left) is int and type(right) is int:
        return left == right
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def _valid_stats(value, classifier_version=None):
    return valid_statistics(value, classifier_version, allow_historical_pending=True)


def _check_subject(selected, group, local_subject):
    if selected is None:
        return
    expected = selected.subject
    valid = (local_subject == expected if not group or local_subject else expected in ("", selected.user))
    if not valid:
        raise ValueError("API portrait ledger subject scope changed")


def _record(value):
    if (not isinstance(value, dict) or set(value) != set(_FACTS) | {"signal", "words", "batchKey"} or
            not _text(value.get("messageId")) or not _position(value.get("position")) or
            not _text(value.get("speaker")) or value.get("sender") not in ("SELF", "OTHER") or
            type(value.get("target")) is not bool or type(value.get("group")) is not bool or
            not isinstance(value.get("localSubject"), str) or
            (not value["group"] and not value["localSubject"]) or
            not _text(value.get("batchKey")) or not isinstance(value.get("words"), dict) or
            any(not _text(word, 120) or not _count(count) for word, count in value["words"].items())):
        raise ValueError("invalid API portrait ledger record")
    _signal(value["signal"])
    if not value["target"] and (value["signal"] is not None or value["words"]):
        raise ValueError("non-target API portrait evidence")
    return deepcopy(value)


def _same_facts(left, right):
    return all(left.get(field) == right.get(field) for field in _FACTS)


def _fragment(value):
    if (not isinstance(value, dict) or set(value) != set(_FACTS) | {"nextPieceIndex"} or
            not _text(value.get("messageId")) or not _position(value.get("position")) or
            not _text(value.get("speaker")) or value.get("sender") not in ("SELF", "OTHER") or
            type(value.get("target")) is not bool or type(value.get("group")) is not bool or
            not isinstance(value.get("localSubject"), str) or
            (not value["group"] and not value["localSubject"]) or
            not _count(value.get("nextPieceIndex"), MAX_PENDING_CHARS) or value["nextPieceIndex"] < 1):
        raise ValueError("invalid API portrait ledger fragment")
    return deepcopy(value)


def statistics_from_records(records, *, classifier_version="api-local-rules-v1", group=False,
                            local_subject, batch_count=0, pending=None):
    """Replay saved classifications in true chronology; never call a model."""
    if type(group) is not bool or not isinstance(local_subject, str) or not group and not local_subject:
        raise ValueError("invalid API portrait ledger subject")
    if not _count(batch_count):
        raise ValueError("invalid API portrait ledger batch count")
    known = {}
    for incoming in records:
        row = _record(incoming)
        if row["group"] != group or row["localSubject"] != local_subject:
            raise ValueError("API portrait ledger subject changed")
        previous = known.get(row["messageId"])
        if previous is not None:
            if not _same_facts(previous, row):
                raise ValueError("API portrait ledger message changed")
            continue
        known[row["messageId"]] = row
    ordered = sorted(known.values(), key=lambda row: (*row["position"], row["messageId"]))
    result = empty_statistics(classifier_version)
    state = result["state"]
    targets = []

    def flush():
        if not targets:
            return
        words = {}
        for row in targets:
            for word, count in row["words"].items():
                words[word] = words.get(word, 0) + count
        last = targets[-1]
        _merge(state, last["signal"], len(targets), (*last["position"], last["messageId"]),
               words, group, local_subject)
        targets.clear()

    for row in ordered:
        if not row["target"]:
            if group and not local_subject and row["sender"] == "SELF":
                state["count"] += 1
            continue
        if row["signal"] is None:
            continue
        if targets and (targets[-1]["batchKey"] != row["batchKey"] or targets[-1]["signal"] != row["signal"]):
            flush()
        targets.append(row)
    flush()
    state["batchCount"] = batch_count
    result["pending"] = deepcopy(pending)
    if not _valid_stats(result, classifier_version):
        raise ValueError("invalid API portrait ledger statistics")
    return result


class ApiPortraitLedger:
    scope = staticmethod(scope)

    def __init__(self, store):
        self.store = store
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.ensure_schema(connection)

    @staticmethod
    def ensure_schema(connection):
        # Individual statements preserve an enclosing checkpoint transaction.
        for statement in _SCHEMA:
            connection.execute(statement)
        if "state_hash" not in {row[1] for row in connection.execute("PRAGMA table_info(api_portrait_ledger_meta_v1)")}:
            connection.execute("ALTER TABLE api_portrait_ledger_meta_v1 ADD COLUMN state_hash TEXT")

    def _metadata(self, connection, selected):
        row = connection.execute("SELECT version,ready,generation,batch_count,revision,prefix_signature,highwater_json,state_hash "
                                 "FROM api_portrait_ledger_meta_v1 WHERE " + _WHERE, selected).fetchone()
        if row is None:
            return {"version": LEDGER_VERSION, "initialized": False, "ready": False, "generation": None,
                    "batchCount": 0, "revision": None, "prefixSignature": None, "highwater": None, "stateHash": None}
        if row[0] != LEDGER_VERSION:
            raise ValueError("incompatible API portrait ledger")
        return {"version": row[0], "initialized": True, "ready": bool(row[1]), "generation": row[2],
                "batchCount": row[3], "revision": row[4], "prefixSignature": row[5],
                "highwater": json.loads(row[6]) if row[6] else None, "stateHash": row[7]}

    def metadata(self, selected):
        selected = _scope(selected)
        with self.store.connect() as connection:
            return self._metadata(connection, selected)

    def _rows(self, connection, selected):
        return [_record(json.loads(row[0])) for row in connection.execute(
            "SELECT record_json FROM api_portrait_ledger_rows_v1 WHERE " + _WHERE +
            " ORDER BY sort_seq,shard,local_id,message_id", selected)]

    def records(self, selected):
        selected = _scope(selected)
        with self.store.connect() as connection:
            return self._rows(connection, selected)

    def _selected_rows(self, connection, selected, identifiers):
        identifiers = sorted(set(identifiers))
        known = {}
        for offset in range(0, len(identifiers), 500):
            batch = identifiers[offset:offset + 500]
            query = ("SELECT record_json FROM api_portrait_ledger_rows_v1 WHERE " + _WHERE +
                     " AND message_id IN (" + ",".join("?" for _identifier in batch) + ")")
            for row in connection.execute(query, (*selected, *batch)):
                record = _record(json.loads(row[0]))
                known[record["messageId"]] = record
        return known

    def known_ids(self, selected):
        selected = _scope(selected)
        with self.store.connect() as connection:
            return {row[0] for row in connection.execute(
                "SELECT message_id FROM api_portrait_ledger_rows_v1 WHERE " + _WHERE, selected)}

    def begin(self, connection, selected, *, reset=False):
        selected = _scope(selected)
        if reset:
            self.clear(connection, selected)
        connection.execute("INSERT OR IGNORE INTO api_portrait_ledger_meta_v1 "
                           "(account,session,source_id,subject,classifier_version,version) VALUES(?,?,?,?,?,?)",
                           (*selected, LEDGER_VERSION))
        return self._metadata(connection, selected)

    def prepare(self, previous_statistics, signal, pieces, *, group=False, local_subject, selected=None):
        if not _valid_stats(previous_statistics) or type(group) is not bool or not isinstance(local_subject, str) or not group and not local_subject:
            raise ValueError("invalid API portrait ledger state")
        if not isinstance(pieces, list) or len(pieces) > 20000:
            raise ValueError("invalid API portrait ledger pieces")
        signal = validate_batch_signal(signal, pieces)
        selected = _scope(selected) if selected is not None else None
        _check_subject(selected, group, local_subject)
        metadata = {"initialized": False, "generation": None, "batchCount": previous_statistics["state"]["batchCount"], "stateHash": None}
        known, fragments = {}, {}
        if selected is not None:
            if previous_statistics["classifierVersion"] != selected.classifier_version:
                raise ValueError("API portrait ledger classifier changed")
            with self.store.connect() as connection:
                connection.execute("BEGIN")
                metadata = self._metadata(connection, selected)
                incoming_ids = [piece["messageId"] for piece in pieces if isinstance(piece, dict) and _text(piece.get("messageId"))]
                known = self._selected_rows(connection, selected, incoming_ids)
                fragments = {row[0]: _fragment(json.loads(row[1])) for row in connection.execute(
                    "SELECT message_id,fragment_json FROM api_portrait_ledger_fragments_v1 WHERE " + _WHERE, selected)}
                if not metadata["initialized"] and (previous_statistics["state"]["count"] or
                        previous_statistics["pending"] is not None or previous_statistics["state"]["batchCount"]):
                    raise ValueError("legacy API portrait needs a new evidence baseline")
                if metadata["stateHash"] is not None:
                    if _state_hash(previous_statistics) != metadata["stateHash"]:
                        raise ValueError("API portrait statistics do not match ledger evidence")
                else:
                    blank = empty_statistics(selected.classifier_version)
                    blank["pending"] = previous_statistics["pending"]
                    baseline = blank if previous_statistics == blank and metadata["batchCount"] == 0 else statistics_from_records(
                        self._rows(connection, selected), classifier_version=selected.classifier_version,
                        group=group, local_subject=local_subject, batch_count=metadata["batchCount"],
                        pending=previous_statistics["pending"])
                    if not _equivalent(previous_statistics, baseline):
                        raise ValueError("API portrait statistics do not match ledger evidence")
        pending = deepcopy(previous_statistics["pending"])
        if pending is not None and pending["messageId"] in known:
            raise ValueError("completed API portrait message cannot remain pending")
        if selected is not None and pending is not None and pending["messageId"] not in fragments:
            raise ValueError("API portrait pending coverage unavailable")
        if len(fragments) > 1:
            raise ValueError("API portrait fragments are not contiguous")
        completed, cleared = {}, set()
        previous_position = None
        accepted_target = False
        append_only = not known
        latest = previous_statistics["state"]["latest"]
        if pending is not None and latest is not None and tuple(pending["position"]) <= tuple(latest[:3]):
            append_only = False
        batch_key = hashlib.sha256(_json([previous_statistics["classifierVersion"],
                                         metadata["generation"], [(piece.get("messageId"), piece.get("_pieceIndex"))
                                          for piece in pieces if isinstance(piece, dict)], signal]).encode("utf-8")).hexdigest()
        for piece in pieces:
            if (not isinstance(piece, dict) or not _text(piece.get("messageId")) or not _text(piece.get("speaker")) or
                    not _position(piece.get("_sort")) or not isinstance(piece.get("text"), str) or
                    not 0 < len(piece["text"]) <= MAX_PENDING_CHARS or type(piece.get("target")) is not bool or
                    type(piece.get("_last")) is not bool or piece.get("sender") not in ("SELF", "OTHER") or
                    not _count(piece.get("_pieceIndex"), MAX_PENDING_CHARS)):
                raise ValueError("invalid API portrait ledger piece")
            position = tuple(piece["_sort"])
            if piece["target"] and latest is not None and position <= tuple(latest[:3]):
                append_only = False
            if previous_position is not None and position < previous_position:
                raise ValueError("API portrait ledger pieces out of order")
            previous_position = position
            identifier = piece["messageId"]
            facts = {"messageId": identifier, "position": list(position), "speaker": piece["speaker"],
                     "sender": piece["sender"], "target": piece["target"], "group": group, "localSubject": local_subject}
            if fragments and identifier not in fragments:
                raise ValueError("unfinished API portrait fragment skipped")
            existing = known.get(identifier) or completed.get(identifier)
            if existing is not None:
                append_only = False
                if not _same_facts(existing, facts) or piece["_pieceIndex"] != 0 or not piece["_last"]:
                    raise ValueError("non-contiguous duplicate API portrait message")
                continue
            fragment = fragments.get(identifier)
            if fragment is None:
                if piece["_pieceIndex"] != 0:
                    if not piece["target"] or pending is None or pending["messageId"] != identifier:
                        raise ValueError("API portrait ledger fragment did not start")
                    fragment = {**facts, "nextPieceIndex": pending["nextPieceIndex"]}
                else:
                    fragment = {**facts, "nextPieceIndex": 0}
            if not _same_facts(fragment, facts) or fragment["nextPieceIndex"] != piece["_pieceIndex"]:
                raise ValueError("non-contiguous API portrait ledger fragments")
            fragment["nextPieceIndex"] += 1
            fragments[identifier] = fragment
            effective, words = None, {}
            if piece["target"]:
                accepted_target = accepted_target or signal is not None
                if pending is None:
                    if piece["_pieceIndex"] != 0:
                        raise ValueError("API portrait ledger pending text unavailable")
                    pending = {"messageId": identifier, "speaker": piece["speaker"], "position": list(position),
                               "nextPieceIndex": 0, "text": "", "signalChars": 0, "weightedResult": None}
                if (pending["messageId"] != identifier or pending["speaker"] != piece["speaker"] or
                        tuple(pending["position"]) != position or pending["nextPieceIndex"] != piece["_pieceIndex"]):
                    raise ValueError("non-contiguous API portrait pending target")
                pending["text"] += piece["text"]
                if len(pending["text"]) > MAX_PENDING_CHARS:
                    raise ValueError("API portrait ledger pending text too long")
                pending["weightedResult"] = _combined_result([
                    (pending["signalChars"], pending["weightedResult"]), (len(piece["text"]), signal)])
                pending["signalChars"] += len(piece["text"]) if signal is not None else 0
                pending["nextPieceIndex"] += 1
                if piece["_last"]:
                    effective = pending["weightedResult"]
                    words = dict(keyword_counts([pending["text"]])) if effective is not None else {}
                    pending = None
            elif pending is not None:
                raise ValueError("API portrait pending target fragment skipped")
            if piece["_last"]:
                completed[identifier] = {**facts, "signal": effective, "words": words, "batchKey": batch_key}
                fragments.pop(identifier, None)
                cleared.add(identifier)
        return {"scope": list(selected) if selected is not None else None,
                "records": list(completed.values()), "pending": pending,
                "fragmentUpdates": list(fragments.values()), "clearFragments": sorted(cleared),
                "batchIncrement": int(accepted_target), "baseGeneration": metadata["generation"],
                "batchCount": metadata["batchCount"] + int(accepted_target),
                "group": group, "localSubject": local_subject, "appendOnly": append_only,
                "statistics": None}

    def statistics(self, selected, prepared=None):
        selected = _scope(selected)
        with self.store.connect() as connection:
            connection.execute("BEGIN")
            metadata = self._metadata(connection, selected)
            records = self._rows(connection, selected)
        if prepared is None:
            group = records[0]["group"] if records else False
            local_subject = records[0]["localSubject"] if records else selected.subject or selected.user
            return statistics_from_records(records, classifier_version=selected.classifier_version,
                                           group=group, local_subject=local_subject, batch_count=metadata["batchCount"])
        if prepared["scope"] is not None and tuple(prepared["scope"]) != selected:
            raise ValueError("API portrait prepared scope changed")
        if prepared["baseGeneration"] is not None and prepared["baseGeneration"] != metadata["generation"]:
            raise ValueError("API portrait ledger changed during preparation")
        return statistics_from_records(records + prepared["records"], classifier_version=selected.classifier_version,
                                       group=prepared["group"], local_subject=prepared["localSubject"],
                                       batch_count=prepared["batchCount"], pending=prepared["pending"])

    def persist(self, connection, selected, prepared=None, *, revision=None, prefix_signature=None,
                highwater=None, ready=None):
        selected = _scope(selected)
        metadata = self._metadata(connection, selected)
        if not metadata["initialized"]:
            self.begin(connection, selected)
            metadata = self._metadata(connection, selected)
        if prepared is not None:
            if prepared["scope"] is not None and tuple(prepared["scope"]) != selected:
                raise ValueError("API portrait prepared scope changed")
            if prepared["baseGeneration"] is not None and prepared["baseGeneration"] != metadata["generation"]:
                raise ValueError("API portrait ledger changed before checkpoint")
            for incoming in prepared["records"]:
                record = _record(incoming)
                previous = connection.execute("SELECT record_json FROM api_portrait_ledger_rows_v1 WHERE " + _WHERE +
                                              " AND message_id=?", (*selected, record["messageId"])).fetchone()
                if previous is not None:
                    if not _same_facts(json.loads(previous[0]), record):
                        raise ValueError("API portrait ledger message changed")
                    continue
                connection.execute("INSERT INTO api_portrait_ledger_rows_v1 "
                                   "(account,session,source_id,subject,classifier_version,message_id,sort_seq,shard,local_id,record_json) "
                                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (*selected, record["messageId"], *record["position"], _json(record)))
            for identifier in prepared["clearFragments"]:
                connection.execute("DELETE FROM api_portrait_ledger_fragments_v1 WHERE " + _WHERE + " AND message_id=?",
                                   (*selected, identifier))
            for incoming in prepared["fragmentUpdates"]:
                fragment = _fragment(incoming)
                connection.execute("INSERT OR REPLACE INTO api_portrait_ledger_fragments_v1 "
                                   "(account,session,source_id,subject,classifier_version,message_id,fragment_json) VALUES(?,?,?,?,?,?,?)",
                                   (*selected, fragment["messageId"], _json(fragment)))
            metadata["batchCount"] = prepared["batchCount"]
            statistics = prepared.get("statistics")
            if statistics is None:
                statistics = statistics_from_records(
                    self._rows(connection, selected), classifier_version=selected.classifier_version,
                    group=prepared["group"], local_subject=prepared["localSubject"],
                    batch_count=prepared["batchCount"], pending=prepared["pending"])
            if (not _valid_stats(statistics, selected.classifier_version) or
                    statistics["pending"] != prepared["pending"] or
                    statistics["state"]["batchCount"] != prepared["batchCount"]):
                raise ValueError("invalid API portrait ledger checkpoint statistics")
            metadata["stateHash"] = _state_hash(statistics)
        if highwater is not None and not _position(highwater):
            raise ValueError("invalid API portrait ledger highwater")
        if ready is not None and type(ready) is not bool:
            raise ValueError("invalid API portrait ledger completion")
        unfinished = bool(prepared is not None and prepared["pending"] is not None or connection.execute(
            "SELECT 1 FROM api_portrait_ledger_fragments_v1 WHERE " + _WHERE + " LIMIT 1", selected).fetchone())
        if ready and unfinished:
            raise ValueError("unfinished API portrait ledger cannot be ready")
        for value in (revision, prefix_signature):
            if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 512):
                raise ValueError("invalid API portrait ledger metadata")
        connection.execute("UPDATE api_portrait_ledger_meta_v1 SET ready=?,generation=generation+1,batch_count=?,"
                           "revision=?,prefix_signature=?,highwater_json=?,state_hash=? WHERE " + _WHERE,
                           (int(metadata["ready"] and not unfinished if ready is None else ready), metadata["batchCount"],
                            metadata["revision"] if revision is None else revision,
                            metadata["prefixSignature"] if prefix_signature is None else prefix_signature,
                            _json(highwater if highwater is not None else metadata["highwater"]), metadata["stateHash"], *selected))

    def clear(self, connection, selected=None, *, account=None, user=None, source_id=None,
              subject=None, classifier_version=None):
        clear_ledger(connection, selected, account=account, user=user, source_id=source_id,
                     subject=subject, classifier_version=classifier_version)


def clear_ledger(connection, selected=None, *, account=None, user=None, source_id=None,
                 subject=None, classifier_version=None):
    """Clear existing ledger tables inside the caller transaction, without schema IO."""
    if selected is not None:
        selected = _scope(selected)
        predicate, parameters = _WHERE, selected
    else:
        if not isinstance(account, str) or not account:
            raise ValueError("API portrait clearing requires an account")
        pairs = [("account", account), ("session", user), ("source_id", source_id),
                 ("subject", subject), ("classifier_version", classifier_version)]
        if any(value is not None and not isinstance(value, str) for _column, value in pairs):
            raise ValueError("invalid API portrait clearing scope")
        clauses, parameters = [], []
        for column, value in pairs:
            if value is None:
                continue
            if column == "source_id" and ":" not in value:
                escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("(source_id=? OR source_id LIKE ? ESCAPE '\\')")
                parameters.extend((value, escaped + ":%"))
            else:
                clauses.append(column + "=?")
                parameters.append(value)
        predicate = " AND ".join(clauses)
        parameters = tuple(parameters)
    present_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in _TABLES:
        if table in present_tables:
            connection.execute("DELETE FROM " + table + " WHERE " + predicate, parameters)

"""Cursor-based reads of one conversation in the existing WeChat snapshot."""
from __future__ import annotations

import base64
import binascii
import heapq
import json
import re
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHARD_NAME = re.compile(r"message__message_\d+\.db\Z")
TABLE_NAME = re.compile(r"Msg_[0-9a-fA-F]{32}\Z")
CURSOR_TEXT = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")
SEARCH_SCAN_LIMIT = 2048
HISTORY_SCAN_LIMIT = 4096
MESSAGE_FIELDS = ("local_id,local_type,real_sender_id,create_time,message_content,"
                  "compress_content,server_id,sort_seq")


def encode_cursor(account, user, position):
    seq, shard, local_id = position
    raw = json.dumps([1, account, user, int(seq), shard, int(local_id)],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(value, account, user):
    if not isinstance(value, str) or not CURSOR_TEXT.fullmatch(value):
        raise ValueError("invalid history cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid history cursor") from exc
    if (not isinstance(parts, list) or len(parts) != 6 or type(parts[0]) is not int or parts[0] != 1 or
            parts[1] != account or parts[2] != user or
            type(parts[3]) is not int or type(parts[5]) is not int or
            not 0 <= parts[3] <= 2**63 - 1 or not 0 <= parts[5] <= 2**63 - 1 or
            not isinstance(parts[4], str) or not SHARD_NAME.fullmatch(parts[4])):
        raise ValueError("invalid history cursor")
    return parts[3], parts[4], parts[5]


def date_bounds(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid history date")
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid history date") from exc
    timezone = ZoneInfo("Asia/Shanghai")
    begin = int(datetime(selected.year, selected.month, selected.day, tzinfo=timezone).timestamp())
    end = begin + 86400
    return begin, end, begin * 1000, end * 1000


def _position(item):
    record, shard, _senders = item
    return int(record[7]), shard, int(record[0])


def _condition(shard, bound, descending):
    if bound is None:
        return None, ()
    seq, bound_shard, local_id = bound
    if shard == bound_shard:
        op = "<" if descending else ">"
        return f"(sort_seq,local_id) {op} (?,?)", (seq, local_id)
    if descending:
        return ("sort_seq <= ?" if shard < bound_shard else "sort_seq < ?"), (seq,)
    return ("sort_seq >= ?" if shard > bound_shard else "sort_seq > ?"), (seq,)


def _merged_rows(db, user, bound=None, descending=True, bounds=None, max_rows=HISTORY_SCAN_LIMIT):
    """Merge keyset SQL cursors; each shard reads only rows reached by this page."""
    found = db._msg_conns(user)
    try:
        streams = []
        for conn, table in found:
            if not TABLE_NAME.fullmatch(table):
                raise RuntimeError("invalid message table")
            shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
            if not SHARD_NAME.fullmatch(shard):
                raise RuntimeError("unidentified message shard")
            streams.append((conn, table, shard))
        names = [entry[2] for entry in streams]
        if len(names) != len(set(names)):
            raise RuntimeError("duplicate message shard")
        ranks = {name: rank for rank, name in enumerate(sorted(names))}
        heap = []
        for conn, table, shard in streams:
            clauses, params = [], []
            clause, values = _condition(shard, bound, descending)
            if clause:
                clauses.append(clause)
                params.extend(values)
            if bounds:
                clauses.append("((create_time >= ? AND create_time < ?) OR "
                               "(create_time >= ? AND create_time < ?))")
                params.extend(bounds)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            direction = "DESC" if descending else "ASC"
            sql = (f"SELECT {MESSAGE_FIELDS} FROM {table}{where} "
                   f"ORDER BY sort_seq {direction},local_id {direction} LIMIT ?")
            cursor = conn.execute(sql, (*params, max_rows + 1))
            senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
            record = cursor.fetchone()
            if record is not None:
                seq, _name, local_id = _position((record, shard, senders))
                key = ((-seq, -ranks[shard], -local_id) if descending else
                       (seq, ranks[shard], local_id))
                heapq.heappush(heap, (key, record, shard, senders, cursor))
        while heap:
            _key, record, shard, senders, cursor = heapq.heappop(heap)
            item = (record, shard, senders)
            following = cursor.fetchone()
            if following is not None:
                seq, _name, local_id = _position((following, shard, senders))
                key = ((-seq, -ranks[shard], -local_id) if descending else
                       (seq, ranks[shard], local_id))
                heapq.heappush(heap, (key, following, shard, senders, cursor))
            yield item
    finally:
        for conn in {id(conn): conn for conn, _table in found}.values():
            conn.close()


def _issue_image(source, account, user, message, item, max_issued_images):
    if message["kind"] == "image" and item[0][1] == 3:
        key = (account, user, message["id"])
        source.issued_images[key] = tuple(message["_sort"]) + (item[0][6],)
        source.issued_images.move_to_end(key)
        if len(source.issued_images) > max_issued_images:
            source.issued_images.popitem(last=False)


def _collect(source, db, user, account, contacts, own_user, *, bound, descending,
             limit, scan_limit, bounds=None, query=None, early_match_scan=None,
             max_issued_images=2048):
    matched, consumed, last = [], 0, None
    with closing(_merged_rows(db, user, bound, descending, bounds, scan_limit)) as rows:
        while consumed < scan_limit and len(matched) < limit:
            if early_match_scan is not None and matched and consumed >= early_match_scan:
                break
            item = next(rows, None)
            if item is None:
                break
            consumed += 1
            last = _position(item)
            message = source._render_row(db, user, item, contacts, own_user)
            if message is None:
                continue
            if query is not None and query not in str(message.get("text") or "").casefold():
                continue
            _issue_image(source, account, user, message, item, max_issued_images)
            matched.append(message)
        more = next(rows, None) is not None
    return matched, last, more


def _target(source, db, user, position, contacts, own_user, max_issued_images):
    seq, shard, local_id = position
    found = db._msg_conns(user)
    try:
        for conn, table in found:
            if not TABLE_NAME.fullmatch(table):
                raise RuntimeError("invalid message table")
            actual_shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
            if not SHARD_NAME.fullmatch(actual_shard):
                raise RuntimeError("unidentified message shard")
            if actual_shard != shard:
                continue
            records = conn.execute(
                f"SELECT {MESSAGE_FIELDS} FROM {table} WHERE sort_seq=? AND local_id=? LIMIT 2",
                (seq, local_id)).fetchall()
            if len(records) != 1:
                break
            senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
            item = (records[0], shard, senders)
            message = source._render_row(db, user, item, contacts, own_user)
            if message is not None:
                _issue_image(source, str(db.account), user, message, item, max_issued_images)
                return message
            break
    finally:
        for conn in {id(conn): conn for conn, _table in found}.values():
            conn.close()
    raise ValueError("invalid history cursor")


def browse(source, account, user, *, before=None, around=None, limit=80,
           max_issued_images=2048):
    if before is not None and around is not None:
        raise ValueError("before and around cannot be combined")
    before_position = decode_cursor(before, account, user) if before is not None else None
    around_position = decode_cursor(around, account, user) if around is not None else None
    with source.lock:
        db = source._db(fresh=True)
        if str(db.account) != account:
            raise ValueError("account mismatch")
        contacts, own_user = source._contacts(db), source.self_user(db)
        if around_position is not None:
            target = _target(source, db, user, around_position, contacts, own_user,
                             max_issued_images)
            older, older_last, more_before = _collect(
                source, db, user, account, contacts, own_user, bound=around_position,
                descending=True, limit=limit // 2, scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            newer, _newer_last, more_after = _collect(
                source, db, user, account, contacts, own_user, bound=around_position,
                descending=False, limit=limit - 1 - len(older), scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            messages = list(reversed(older)) + [target] + newer
            next_position = older_last or around_position
        else:
            selected, next_position, more_before = _collect(
                source, db, user, account, contacts, own_user, bound=before_position,
                descending=True, limit=limit, scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            messages = list(reversed(selected))
            more_after = before_position is not None
        return {"messages": messages, "hasMoreBefore": more_before,
                "hasMoreAfter": more_after,
                "nextCursor": encode_cursor(account, user, next_position) if next_position else None,
                "oldestCursor": messages[0]["historyCursor"] if messages else None,
                "newestCursor": messages[-1]["historyCursor"] if messages else None,
                "focusId": target["id"] if around_position is not None else None}


def search(source, account, user, *, query=None, day=None, before=None, limit=50,
           max_issued_images=2048):
    if query is not None and (not isinstance(query, str) or len(query) > 256 or
                              any(ord(char) < 32 for char in query)):
        raise ValueError("invalid history query")
    query = (query or "").strip().casefold()
    bounds = date_bounds(day)
    if not query and bounds is None:
        raise ValueError("query or date required")
    before_position = decode_cursor(before, account, user) if before is not None else None
    with source.lock:
        db = source._db(fresh=True)
        if str(db.account) != account:
            raise ValueError("account mismatch")
        contacts, own_user = source._contacts(db), source.self_user(db)
        messages, last, more = _collect(
            source, db, user, account, contacts, own_user, bound=before_position,
            descending=True, limit=limit, scan_limit=SEARCH_SCAN_LIMIT,
            bounds=bounds, query=query if query else None,
            early_match_scan=256 if query else None,
            max_issued_images=max_issued_images)
        return {"messages": messages, "hasMore": more,
                "nextCursor": encode_cursor(account, user, last) if more and last else None}


def saved_results(store, account, user, version, messages):
    ids = list(dict.fromkeys(message["id"] for message in messages))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    args = [account, user, version, *ids]
    with store.connect() as conn:
        analyzed = conn.execute(
            f"SELECT id,result FROM results_v2 WHERE account=? AND session=? AND version=? "
            f"AND id IN ({placeholders})", args).fetchall()
        skipped = conn.execute(
            f"SELECT id,reason FROM analysis_skips WHERE account=? AND session=? AND version=? "
            f"AND id IN ({placeholders})", args).fetchall()
    return ({stable_id: {key: value for key, value in json.loads(raw).items() if key != "score"}
             for stable_id, raw in analyzed} |
            {stable_id: {"state": "skipped", "reason": reason} for stable_id, reason in skipped})

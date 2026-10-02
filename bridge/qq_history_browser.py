"""Bounded local QQ history/search; only the canonical account library is read."""
from __future__ import annotations

import re
import time
from datetime import date,datetime,timedelta
from qq_source import decode_cursor, encode_cursor, render_message

HISTORY_SCAN_LIMIT = 4096
SEARCH_SCAN_LIMIT = 2048


def date_bounds(value):
    # QQ's page renders Date in the device's local timezone. Keep date filtering
    # consistent with that calendar, including historical daylight-saving rules.
    if value is None:
        return None
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value):
        raise ValueError('invalid history date')
    try:
        selected=date.fromisoformat(value)
        begin=datetime.combine(selected,datetime.min.time())
        end=begin+timedelta(days=1)
        start_s=int(time.mktime(begin.timetuple()));end_s=int(time.mktime(end.timetuple()))
    except (ValueError,OverflowError,OSError):
        raise ValueError('invalid history date') from None
    return start_s,end_s,start_s*1000,end_s*1000


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid history limit")


def _collect(store, account, user, *, bound=None, descending=True, limit=80,
             scan_limit=HISTORY_SCAN_LIMIT, bounds=None, query=None, revision=1,source=None,member=None):
    group=source is not None and source.conversation_kind(user)=='group'
    clauses = ["account_key=?", "conversation_key=?"]
    if not group:
        clauses.append("direction IN ('self','peer')")
    values = [account, user]
    if member is not None:
        uin,uid=source.member_filter(user,member)
        clauses.append("((? IS NOT NULL AND LTRIM(sender_uin,'0')=?) OR (sender_uin IS NULL AND ? IS NOT NULL AND sender_uid=?))")
        values.extend((uin,uin,uid,uid))
    if bound is not None:
        clauses.append("(time_ms,local_seq) " + ("<" if descending else ">") + " (?,?)")
        values += [bound[0], bound[2]]
    if bounds is not None:
        clauses.append("time_ms>=? AND time_ms<?")
        values += [bounds[2], bounds[3]]
    direction = "DESC" if descending else "ASC"
    sql = ("SELECT * FROM messages WHERE " + " AND ".join(clauses) +
           f" ORDER BY time_ms {direction},local_seq {direction} LIMIT ?")
    cursor = store.connection.execute(sql, (*values, scan_limit + 1))
    messages, last, consumed = [], None, 0
    try:
        while len(messages) < limit and consumed < scan_limit:
            row = cursor.fetchone()
            if row is None:
                break
            consumed += 1
            last = (row["time_ms"], "qq:" + user, row["local_seq"])
            rendered = source.project_message(row,revision) if source is not None else render_message(account, row, revision)
            if rendered is not None and (query is None or query in rendered["text"].casefold()):
                messages.append(rendered)
        more = cursor.fetchone() is not None
        return messages, last, more
    finally:
        cursor.close()


def browse(source, account, user, *, before=None, around=None, limit=80, max_issued_images=2048,member=None):
    _limit(limit, 500)
    if before is not None and around is not None:
        raise ValueError("before and around cannot be combined")
    with source.read_library(account) as (_account, store):
        state = store.revision(account, user)
        revision = state[0] if state is not None else 1
        before_position = decode_cursor(before, account, user, revision) if before is not None else None
        around_position = decode_cursor(around, account, user, revision) if around is not None else None
        if around_position is not None:
            row = store.connection.execute(
                "SELECT * FROM messages WHERE account_key=? AND conversation_key=? AND time_ms=? AND local_seq=?",
                (account, user, around_position[0], around_position[2])).fetchone()
            target = source.project_message(row,revision) if row is not None else None
            if target is None:
                raise ValueError("invalid history cursor")
            if member is not None and target.get('senderId')!=member:
                raise ValueError('history-anchor-member-mismatch')
            older, older_last, more_before = _collect(store, account, user, bound=around_position, limit=limit // 2, revision=revision,source=source,member=member)
            newer, _, more_after = _collect(store, account, user, bound=around_position,
                                            descending=False, limit=limit - 1 - len(older), revision=revision,source=source,member=member)
            messages = list(reversed(older)) + [target] + newer
            next_position = older_last or around_position
        else:
            selected, next_position, more_before = _collect(store, account, user, bound=before_position, limit=limit, revision=revision,source=source,member=member)
            messages = list(reversed(selected))
            more_after = before_position is not None
        return {"messages": messages, "hasMoreBefore": more_before, "hasMoreAfter": more_after,
                "nextCursor": encode_cursor(account, user, next_position, revision) if next_position else None,
                "oldestCursor": messages[0]["historyCursor"] if messages else None,
                "newestCursor": messages[-1]["historyCursor"] if messages else None,
                "focusId": target["id"] if around_position is not None else None}


def search(source, account, user, *, query=None, day=None, before=None, limit=50, max_issued_images=2048,member=None):
    _limit(limit, 500)
    if query is not None and (not isinstance(query, str) or len(query) > 256 or
                              any(ord(char) < 32 or ord(char) == 127 for char in query)):
        raise ValueError("invalid history query")
    query = (query or "").strip().casefold()
    bounds = date_bounds(day)
    if not query and bounds is None and member is None:
        raise ValueError("query or date required")
    with source.read_library(account) as (_account, store):
        state = store.revision(account, user)
        revision = state[0] if state is not None else 1
        position = decode_cursor(before, account, user, revision) if before is not None else None
        messages, last, more = _collect(store, account, user, bound=position, limit=limit,
                                        scan_limit=SEARCH_SCAN_LIMIT, bounds=bounds, query=query or None, revision=revision,source=source,member=member)
        return {"messages": messages, "hasMore": more,
                "nextCursor": encode_cursor(account, user, last, revision) if more and last else None}

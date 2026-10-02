"""Read-only SQLite query layer over decrypted (or fixture) database images.

The SAME code path serves live decrypted shards and synthetic fixture shards. Correctness contract:
- real SQL keyset pagination on `(sort_seq, shardId, local_id)` with per-shard predicates + LIMIT,
- cross-shard merge with full composite cursor (shard tie handling),
- `create_time` seconds normalised to Unix milliseconds,
- direction fail-closed to `unknown` when the sender is absent from Name2Id,
- `compress_content` fallback + Zstd detection, exact text preservation (no silent replacement),
- 64-bit ids/sequences as decimal strings; duplicate text preserved as distinct rows.

No text is written to logs; rows are returned to the caller only.
"""

from __future__ import annotations

import hashlib
import heapq
import sqlite3
import zstandard
from dataclasses import dataclass
from typing import Iterator

from . import errors

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SECONDS_MAX = 100_000_000_000  # below this, a create_time is seconds, not ms

KIND_TEXT = "text"
KIND_NON_TEXT = "nontext"
KIND_SYSTEM = "system"

SYSTEM_TYPES = {10000, 10002}

# SQL expression that normalises stored create_time to milliseconds.
TIME_MS_SQL = "(CASE WHEN create_time < 100000000000 THEN create_time * 1000 ELSE create_time END)"


def md5_username(username: str) -> str:
    return hashlib.md5(username.encode("utf-8")).hexdigest()


def contact_id_for(account_id: str, username: str) -> str:
    return hashlib.sha256(f"{account_id}\x00{username}".encode("utf-8")).hexdigest()[:32]


def message_id_for(account_id: str, username: str, shard_id: str, local_id: int) -> str:
    """Canonical, stable row locator identity: account + contact + shard + local.

    It never depends on the mutable server_id, so a SELF message keeps one identity when its
    server id upgrades from 0 to the assigned value. `server_id` is kept as a secondary attribute
    for cross-shard sync de-duplication (see `ContactStore._dedupe`).
    """
    basis = f"{account_id}\x00{username}\x00{shard_id}\x00l{local_id}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def normalize_time(value: object) -> int:
    """Seconds -> milliseconds; already-ms values pass through."""
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return number * 1000 if number < SECONDS_MAX else number


def extract_text(message_content: object, compress_content: object) -> str | None:
    """Return exact text, or None when the body cannot be faithfully decoded (=> nontext)."""
    raw: bytes | None = None
    if isinstance(message_content, str):
        if message_content != "":
            raw = message_content.encode("utf-8", "surrogatepass")
    elif isinstance(message_content, (bytes, bytearray)) and len(message_content) > 0:
        raw = bytes(message_content)
    if raw is None and isinstance(compress_content, (bytes, bytearray)) and len(compress_content) > 0:
        raw = bytes(compress_content)
    if raw is None:
        return ""
    if raw[:4] == ZSTD_MAGIC:
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw)
        except zstandard.ZstdError:
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def classify_kind(local_type: int) -> str:
    if local_type in SYSTEM_TYPES:
        return KIND_SYSTEM
    if local_type == 1:
        return KIND_TEXT
    return KIND_NON_TEXT


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


class SqliteShard:
    """One deserialized shard database for one account."""

    def __init__(self, shard_id: str, image: bytes, account_id: str, self_username: str | None) -> None:
        self.shard_id = shard_id
        self.account_id = account_id
        self.image = image
        try:
            self._conn = sqlite3.connect(":memory:")
            self._conn.deserialize(image)
        except sqlite3.Error as exc:
            raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "sqlite deserialize failed") from exc
        self._name_to_id: dict[str, int] = {}
        self._id_to_name: dict[int, str] = {}
        self._message_tables: dict[str, str] = {}
        self._columns: dict[str, set[str]] = {}
        self.self_id: int | None = None
        self._discover()
        if self_username and self_username in self._name_to_id:
            self.self_id = self._name_to_id[self_username]

    # -- discovery ---------------------------------------------------------

    def _discover(self) -> None:
        names = self._table_names()
        if "Name2Id" not in names:
            raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "Name2Id missing")
        name_columns = self._table_columns("Name2Id")
        id_column = "id" if "id" in name_columns else "rowid"
        for row in self._execute(f"SELECT {id_column}, user_name FROM Name2Id"):
            if row[1] is None:
                continue
            self._name_to_id[str(row[1])] = int(row[0])
            self._id_to_name[int(row[0])] = str(row[1])
        for username in self._name_to_id:
            table = f"Msg_{md5_username(username)}"
            if table in names:
                self._message_tables[username] = table
                self._columns[table] = set(self._table_columns(table))

    def _table_names(self) -> set[str]:
        return {str(row[0]) for row in self._execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _table_columns(self, table: str) -> set[str]:
        return {str(row[1]) for row in self._execute(f'PRAGMA table_info("{table}")')}

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            return list(self._conn.execute(sql, params))
        except sqlite3.Error as exc:
            raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "query failed") from exc

    # -- contact helpers ---------------------------------------------------

    def usernames(self) -> list[str]:
        return list(self._message_tables.keys())

    def known_usernames(self) -> list[str]:
        """Every Name2Id username (self included), for account self cross-validation."""
        return list(self._name_to_id.keys())

    def set_self_username(self, username: str | None) -> bool:
        """Bind the resolved self username; returns True when it exists in Name2Id."""
        self.self_id = self._name_to_id.get(username) if username else None
        return self.self_id is not None

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def contact_metadata(self) -> dict[str, dict]:
        """Best-effort display-name map from a contact/session metadata database.

        Only tables that expose a `username` column and a nickname/remark column are read; the
        Name2Id message role is never required here (metadata DBs are not message shards). An
        unknown schema yields {} rather than an error, and usernames stay separate from opaque ids.
        """
        out: dict[str, dict] = {}
        for table in sorted(self._table_names()):
            try:
                columns = self._table_columns(table)
            except errors.ProtocolError:
                continue
            if "username" not in columns:
                continue
            name_col = _first_existing(
                columns, ("remark", "nick_name", "nickname", "alias", "display_name")
            )
            if name_col is None:
                continue
            try:
                rows = self._execute(f'SELECT "username", "{name_col}" FROM "{table}"')
            except errors.ProtocolError:
                continue
            for row in rows:
                if row[0] is None:
                    continue
                username = str(row[0])
                entry = out.setdefault(username, {})
                if row[1]:
                    entry["name"] = str(row[1])
        return out

    def has_table(self, username: str) -> bool:
        return username in self._message_tables

    def count(self, username: str, cutoff_ms: int) -> int:
        table = self._message_tables.get(username)
        if not table:
            return 0
        row = self._execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {TIME_MS_SQL} <= ?', (cutoff_ms,)
        )
        return int(row[0][0]) if row else 0

    def _select_columns(self, table: str) -> tuple[str, list[str]]:
        columns = self._columns[table]
        local_col = _first_existing(columns, ("local_id", "localId"))
        server_col = _first_existing(columns, ("server_id", "serverId"))
        sort_col = _first_existing(columns, ("sort_seq", "sortSeq"))
        type_col = _first_existing(columns, ("local_type", "localType"))
        sender_col = _first_existing(columns, ("real_sender_id", "realSenderId", "sender_id"))
        time_col = _first_existing(columns, ("create_time", "createTime"))
        body_col = _first_existing(columns, ("message_content", "messageContent", "content"))
        compress_col = _first_existing(columns, ("compress_content", "compressContent"))
        required = [local_col, sort_col, type_col]
        if any(column is None for column in required):
            raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "message columns missing")
        selected = [column for column in (local_col, server_col, sort_col, type_col, sender_col, time_col, body_col, compress_col) if column]
        return table, selected

    def _row_from(self, selected: list[str], record: tuple, username: str, contact_id: str) -> dict:
        values = dict(zip(selected, record))
        local_id = int(values.get("local_id") or values.get("localId") or 0)
        server_id = int(values.get("server_id") or values.get("serverId") or 0)
        sort_seq = int(values.get("sort_seq") or values.get("sortSeq") or 0)
        local_type = int(values.get("local_type") or values.get("localType") or 0)
        sender_raw = values.get("real_sender_id", values.get("realSenderId", values.get("sender_id")))
        create_time = values.get("create_time", values.get("createTime"))
        body = values.get("message_content", values.get("messageContent", values.get("content")))
        compress = values.get("compress_content", values.get("compressContent"))

        kind = classify_kind(local_type)
        text = ""
        if kind == KIND_TEXT:
            extracted = extract_text(body, compress)
            if extracted is None:
                kind = KIND_NON_TEXT
            else:
                text = extracted

        sender_id = int(sender_raw) if sender_raw is not None else None
        if kind != KIND_TEXT:
            side = "unknown"
        elif self.self_id is None or sender_id is None:
            side = "unknown"
        elif sender_id == self.self_id:
            side = "self"
        elif sender_id in self._id_to_name:
            side = "other"
        else:
            side = "unknown"

        return {
            "id": message_id_for(self.account_id, username, self.shard_id, local_id),
            "accountId": self.account_id,
            "contactId": contact_id,
            "shardId": self.shard_id,
            "localId": str(local_id),
            "serverId": str(server_id),
            "sortSeq": str(sort_seq),
            "senderId": str(sender_id) if sender_id is not None else "0",
            "localType": local_type,
            "kind": kind,
            "time": normalize_time(create_time),
            "text": text,
            "side": side,
        }

    def _keyset_predicate(self, cursor: tuple[int, str, int] | None) -> tuple[str, tuple]:
        if cursor is None:
            return "", ()
        cursor_sort, cursor_shard, cursor_local = cursor
        if self.shard_id > cursor_shard:
            return "AND sort_seq >= ?", (cursor_sort,)
        if self.shard_id < cursor_shard:
            return "AND sort_seq > ?", (cursor_sort,)
        return "AND (sort_seq > ? OR (sort_seq = ? AND local_id > ?))", (
            cursor_sort,
            cursor_sort,
            cursor_local,
        )

    def iter_keyset(
        self,
        username: str,
        contact_id: str,
        cutoff_ms: int,
        cursor: tuple[int, str, int] | None,
        limit: int,
    ) -> Iterator[dict]:
        table = self._message_tables.get(username)
        if not table:
            return
        table, selected = self._select_columns(table)
        predicate, predicate_params = self._keyset_predicate(cursor)
        columns = ", ".join(f'"{column}"' for column in selected)
        sql = (
            f'SELECT {columns} FROM "{table}" WHERE {TIME_MS_SQL} <= ? {predicate} '
            f"ORDER BY sort_seq ASC, local_id ASC LIMIT ?"
        )
        params = (cutoff_ms, *predicate_params, limit)
        try:
            cursor_obj = self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "keyset query failed") from exc
        for record in cursor_obj:
            yield self._row_from(selected, tuple(record), username, contact_id)

    def latest(self, username: str, contact_id: str, limit: int) -> list[dict]:
        table = self._message_tables.get(username)
        if not table:
            return []
        table, selected = self._select_columns(table)
        columns = ", ".join(f'"{column}"' for column in selected)
        sql = f'SELECT {columns} FROM "{table}" ORDER BY sort_seq DESC, local_id DESC LIMIT ?'
        rows = self._execute(sql, (limit,))
        return [self._row_from(selected, tuple(record), username, contact_id) for record in rows]

    def last_key(self, username: str, cutoff_ms: int) -> tuple[int, int] | None:
        """Composite (sort_seq, local_id) of the newest row at/before `cutoff_ms`, or None."""
        table = self._message_tables.get(username)
        if not table:
            return None
        columns = self._columns[table]
        local_col = _first_existing(columns, ("local_id", "localId"))
        sort_col = _first_existing(columns, ("sort_seq", "sortSeq"))
        if local_col is None or sort_col is None:
            return None
        sql = (
            f'SELECT "{sort_col}", "{local_col}" FROM "{table}" WHERE {TIME_MS_SQL} <= ? '
            f'ORDER BY "{sort_col}" DESC, "{local_col}" DESC LIMIT 1'
        )
        rows = self._execute(sql, (cutoff_ms,))
        if not rows:
            return None
        return (int(rows[0][0] or 0), int(rows[0][1] or 0))

    def _before_predicate(self, cursor: tuple[int, str, int], inclusive: bool) -> tuple[str, tuple]:
        cursor_sort, cursor_shard, cursor_local = cursor
        if self.shard_id < cursor_shard:
            return "AND sort_seq <= ?", (cursor_sort,)
        if self.shard_id > cursor_shard:
            return "AND sort_seq < ?", (cursor_sort,)
        if inclusive:
            return "AND (sort_seq < ? OR (sort_seq = ? AND local_id <= ?))", (
                cursor_sort,
                cursor_sort,
                cursor_local,
            )
        return "AND (sort_seq < ? OR (sort_seq = ? AND local_id < ?))", (
            cursor_sort,
            cursor_sort,
            cursor_local,
        )

    def iter_before(
        self,
        username: str,
        contact_id: str,
        cutoff_ms: int,
        cursor: tuple[int, str, int],
        limit: int,
        inclusive: bool = True,
    ) -> Iterator[dict]:
        """Up to `limit` rows at/before (`inclusive`) or strictly before `cursor`, oldest-first."""
        table = self._message_tables.get(username)
        if not table or limit <= 0:
            return
        table, selected = self._select_columns(table)
        predicate, predicate_params = self._before_predicate(cursor, inclusive)
        columns = ", ".join(f'"{column}"' for column in selected)
        sql = (
            f'SELECT {columns} FROM "{table}" WHERE {TIME_MS_SQL} <= ? {predicate} '
            f"ORDER BY sort_seq DESC, local_id DESC LIMIT ?"
        )
        params = (cutoff_ms, *predicate_params, limit)
        try:
            cursor_obj = self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "context query failed") from exc
        rows = [self._row_from(selected, tuple(record), username, contact_id) for record in cursor_obj]
        rows.reverse()
        yield from rows


class ContactStore:
    """Aggregates all shards of one account; honours explicit contact ids when provided."""

    def __init__(
        self,
        account_id: str,
        shards: list[SqliteShard],
        contact_ids: dict[str, str] | None = None,
        contact_meta: dict[str, dict] | None = None,
    ) -> None:
        self.account_id = account_id
        self.shards = shards
        self._contact_ids = contact_ids or {}
        self._contact_meta = contact_meta or {}
        self._usernames: dict[str, list[SqliteShard]] = {}
        # server_id -> owning shard, for explicit cross-shard re-sync de-duplication.
        self._server_shard: dict[str, str] = {}
        for shard in shards:
            for username in shard.usernames():
                self._usernames.setdefault(username, []).append(shard)

    def close(self) -> None:
        for shard in self.shards:
            shard.close()

    def _dedupe(self, rows: list[dict]) -> list[dict]:
        """Drop a row that duplicates an earlier nonzero server id from a DIFFERENT shard
        (cross-shard re-sync). Distinct local ids, same-shard repeats and duplicates-by-text are
        always preserved."""
        kept: list[dict] = []
        for row in rows:
            server = row.get("serverId", "0")
            shard = row.get("shardId", "")
            if server not in ("", "0"):
                owner = self._server_shard.get(server)
                if owner is not None and owner != shard:
                    continue
                self._server_shard[server] = shard
            kept.append(row)
        return kept

    def contact_id_for(self, username: str) -> str:
        return self._contact_ids.get(username) or contact_id_for(self.account_id, username)

    def username_for(self, contact_id: str) -> str | None:
        for username in self._usernames:
            if self.contact_id_for(username) == contact_id:
                return username
        return None

    def contacts(self) -> list[dict]:
        contacts: list[dict] = []
        for username, shards in sorted(self._usernames.items()):
            count = 0
            latest: int | None = None
            for shard in shards:
                count += shard.count(username, 1 << 62)
                rows = shard.latest(username, self.contact_id_for(username), 1)
                if rows:
                    candidate = rows[0]["time"]
                    if latest is None or candidate > latest:
                        latest = candidate
            if count <= 0:
                continue
            meta = self._contact_meta.get(username, {})
            contacts.append(
                {
                    "id": self.contact_id_for(username),
                    "accountId": self.account_id,
                    "name": meta.get("name") or username,
                    "kind": meta.get("kind") or self._kind(username),
                    "messageCount": count,
                    "lastTime": latest,
                }
            )
        return contacts

    def _kind(self, username: str) -> str:
        if username.endswith("@chatroom"):
            return "group"
        if username.startswith("gh_"):
            return "service"
        if not username:
            return "unknown"
        return "friend"

    def total(self, username: str, cutoff_ms: int) -> int:
        return sum(shard.count(username, cutoff_ms) for shard in self._shards_for(username))

    def rows(self, username: str, cutoff_ms: int = 1 << 62) -> Iterator[dict]:
        """Stream every row for a username in deterministic order (bounded pages)."""
        cursor: tuple[int, str, int] | None = None
        while True:
            batch, cursor, done = self.page(username, cutoff_ms, cursor, 200)
            yield from batch
            if done:
                break

    def _shards_for(self, username: str) -> list[SqliteShard]:
        return self._usernames.get(username, [])

    def page(
        self,
        username: str,
        cutoff_ms: int,
        cursor: tuple[int, str, int] | None,
        limit: int,
    ) -> tuple[list[dict], tuple[int, str, int] | None, bool]:
        contact_id = self.contact_id_for(username)
        # Fetch one extra row per shard so a full page can still detect whether more follows.
        per_shard = [
            shard.iter_keyset(username, contact_id, cutoff_ms, cursor, limit + 1)
            for shard in self._shards_for(username)
        ]
        merged = heapq.merge(
            *per_shard,
            key=lambda row: (int(row["sortSeq"]), str(row["shardId"]), int(row["localId"])),
        )
        rows: list[dict] = []
        last_key: tuple[int, str, int] | None = None
        following: dict | None = None
        for row in merged:
            server = row.get("serverId", "0")
            shard_id = str(row["shardId"])
            if server not in ("", "0"):
                owner = self._server_shard.get(server)
                if owner is not None and owner != shard_id:
                    continue
                self._server_shard[server] = shard_id
            if len(rows) >= limit:
                following = row
                break
            rows.append(row)
            last_key = (int(row["sortSeq"]), shard_id, int(row["localId"]))
        if following is None:
            return rows, None, True
        return rows, last_key, False

    def context_before(
        self,
        username: str,
        cutoff_ms: int,
        cursor: tuple[int, str, int] | None,
        limit: int,
        inclusive: bool = True,
    ) -> list[dict]:
        """Up to `limit` rows at/before (`inclusive`) or strictly before `cursor`, oldest-first."""
        if cursor is None or limit <= 0:
            return []
        contact_id = self.contact_id_for(username)
        gathered: list[dict] = []
        for shard in self._shards_for(username):
            gathered.extend(shard.iter_before(username, contact_id, cutoff_ms, cursor, limit, inclusive))
        gathered.sort(key=lambda row: (int(row["sortSeq"]), str(row["shardId"]), int(row["localId"])))
        deduped = self._dedupe(gathered)
        return deduped[-limit:] if len(deduped) > limit else deduped

    def latest(self, username: str, limit: int) -> list[dict]:
        contact_id = self.contact_id_for(username)
        gathered: list[dict] = []
        for shard in self._shards_for(username):
            gathered.extend(shard.latest(username, contact_id, limit))
        gathered.sort(key=lambda row: (int(row["sortSeq"]), str(row["shardId"]), int(row["localId"])))
        deduped = self._dedupe(gathered)
        return deduped[-limit:] if len(deduped) > limit else deduped

    def last_key(self, username: str, cutoff_ms: int) -> tuple[int, str, int] | None:
        """Newest composite snapshot key ``(sortSeq, shardId, localId)`` at/before `cutoff_ms`."""
        best: tuple[int, str, int] | None = None
        for shard in self._shards_for(username):
            key = shard.last_key(username, cutoff_ms)
            if key is None:
                continue
            candidate = (key[0], shard.shard_id, key[1])
            if best is None or candidate > best:
                best = candidate
        return best


def encode_cursor(cursor: tuple[int, str, int] | None) -> dict | None:
    if cursor is None:
        return None
    return {"sortSeq": str(cursor[0]), "shardId": cursor[1], "localId": str(cursor[2])}


def decode_cursor(raw: object) -> tuple[int, str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise errors.ProtocolError(errors.BAD_REQUEST, "cursor must be an object")
    try:
        return (int(str(raw["sortSeq"])), str(raw["shardId"]), int(str(raw["localId"])))
    except (KeyError, ValueError) as exc:
        raise errors.ProtocolError(errors.BAD_REQUEST, "invalid cursor") from exc


def sqlite_image_from_pages(
    pages: dict[int, bytes],
    page_size: int = 4096,
    reserved: int = 80,
    commit_pages: int | None = None,
) -> bytes:
    """Assemble a valid SQLite file image from decrypted usable pages (reserved bytes zeroed).

    When `commit_pages` is given (a WAL commit's database size), the image is truncated to exactly
    that many pages and every page must be present; a missing committed page is an error rather
    than a silently short image.
    """
    if not pages:
        raise errors.ProtocolError(errors.READ_ERROR, "no pages")
    if commit_pages is not None:
        if commit_pages < 1:
            raise errors.ProtocolError(errors.READ_ERROR, "invalid committed page count")
        for page_number in range(1, commit_pages + 1):
            if page_number not in pages:
                raise errors.ProtocolError(errors.READ_ERROR, "committed page missing")
        ordered = [pages[page_number] for page_number in range(1, commit_pages + 1)]
    else:
        ordered = [pages[page_number] for page_number in sorted(pages)]
    out = bytearray()
    for usable in ordered:
        if len(usable) != page_size - reserved:
            raise errors.ProtocolError(errors.READ_ERROR, "unexpected usable page size")
        out += usable + b"\x00" * reserved
    return bytes(out)

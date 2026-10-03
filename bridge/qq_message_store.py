"""Per-account QQ message library (migration contract T05, local-core-v1).

Owns the schema documented in ``docs/migration/data-schema.md`` and the only write
path into it. ``bridge/test_qq_message_store.py`` compares ``SCHEMA_SQL`` statement
by statement with the documented DDL, so code and contract cannot drift apart.

Local sequence numbers are immutable, unique per conversation and allowed to have
gaps (C05-R3): nothing here ever renumbers an existing row. Backfill still raises
``data_revision`` and records the full ``(time_ms, local_seq)`` affected position,
because not reordering is not the same as not recomputing (C07-R2).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager, closing, nullcontext
from pathlib import Path

from product_profile import current_product
from qq_identity import account_key, canonical_uin
from account_store import _check_root, _regular

SCHEMA_VERSION = 4
DATABASE_NAME = "messages.sqlite"

SCHEMA_SQL_V3 = """
CREATE TABLE conversations (
  conversation_key TEXT PRIMARY KEY,
  account_key      TEXT NOT NULL,
  peer_uin         TEXT,
  peer_uid         TEXT,
  display_name     TEXT,
  kind             TEXT NOT NULL CHECK (kind IN ('friend')),
  selected         INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0,1)),
  first_time_ms    INTEGER,
  last_time_ms     INTEGER,
  data_revision    INTEGER NOT NULL DEFAULT 1,
  earliest_affected_time_ms INTEGER,
  earliest_affected_local_seq INTEGER,
  UNIQUE (account_key, conversation_key)
);
CREATE TABLE messages (
  message_key      TEXT PRIMARY KEY,
  account_key      TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  native_id_kind   TEXT NOT NULL,
  native_id        TEXT NOT NULL,
  native_seq       TEXT,
  local_seq        INTEGER NOT NULL CHECK (typeof(local_seq) = 'integer' AND local_seq >= 1),
  sender_uin       TEXT,
  sender_uid       TEXT,
  send_type        TEXT,
  direction        TEXT NOT NULL CHECK (direction IN ('self','peer','system','conflict')),
  time_ms          INTEGER NOT NULL,
  msg_type         INTEGER,
  kind             TEXT NOT NULL,
  text             TEXT,
  quote            TEXT,
  status           TEXT NOT NULL DEFAULT 'normal'
                     CHECK (status IN ('normal','recalled','revised','conflict')),
  recall_time      TEXT,
  revision         INTEGER NOT NULL DEFAULT 1,
  raw              TEXT,
  normalize_version TEXT NOT NULL,
  UNIQUE (account_key, conversation_key, native_id_kind, native_id),
  UNIQUE (account_key, conversation_key, local_seq),
  FOREIGN KEY (account_key, conversation_key)
    REFERENCES conversations (account_key, conversation_key)
);
CREATE INDEX idx_messages_conv_order
  ON messages (account_key, conversation_key, time_ms, local_seq);
CREATE TABLE message_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
  native_id_kind TEXT NOT NULL, native_id TEXT NOT NULL,
  existing_revision INTEGER NOT NULL, incoming_raw TEXT NOT NULL,
  detected_at INTEGER NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1))
);
CREATE TABLE message_observations (
  message_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (message_key, fingerprint),
  FOREIGN KEY (message_key) REFERENCES messages (message_key)
);
CREATE TABLE sync_checkpoints (
  account_key TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  cursor_version INTEGER NOT NULL,
  platform TEXT NOT NULL CHECK (platform = 'qq'),
  window_start_ms INTEGER NOT NULL,
  window_end_ms INTEGER NOT NULL,
  scanned_through_ms INTEGER NOT NULL DEFAULT 0,
  last_commit_time_ms INTEGER NOT NULL DEFAULT 0,
  last_commit_count INTEGER NOT NULL DEFAULT 0,
  last_task_status TEXT NOT NULL CHECK
    (last_task_status IN ('complete','complete-empty','partial','error')),
  attempts INTEGER NOT NULL DEFAULT 0,
  overlap_ms INTEGER NOT NULL DEFAULT 2000,
  state TEXT NOT NULL CHECK (state IN
    ('IDLE','FETCHING','STAGING','COMMITTED','ERROR','DISCONNECTED','CATCHING_UP','PARTIAL')),
  last_error TEXT,
  partial_reason TEXT,
  UNIQUE (account_key, conversation_key),
  FOREIGN KEY (account_key, conversation_key)
    REFERENCES conversations (account_key, conversation_key)
);
CREATE TABLE identity_aliases (
  account_key TEXT NOT NULL,
  uin TEXT NOT NULL, uid TEXT NOT NULL DEFAULT '', peer_uid TEXT NOT NULL DEFAULT '',
  alias_kind TEXT NOT NULL,
  evidence TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('confirmed','probable','pending')),
  created_at INTEGER NOT NULL,
  UNIQUE (account_key, uin, uid, peer_uid, alias_kind)
);
CREATE TABLE import_manifest (
  import_id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  account_key TEXT NOT NULL,
  rows_total INTEGER, rows_ok INTEGER, rows_rejected INTEGER, rows_conflict INTEGER,
  earliest_native_time_ms INTEGER, latest_native_time_ms INTEGER,
  imported_at INTEGER NOT NULL,
  schema_version TEXT NOT NULL
);
"""

from qq_entities import DDL as ENTITY_SQL, conversation_identity, upsert_person, avatar
SCHEMA_SQL = SCHEMA_SQL_V3.replace("kind             TEXT NOT NULL CHECK (kind IN ('friend')),",
    "kind             TEXT NOT NULL CHECK (kind IN ('friend','group')),\n  group_code       TEXT,\n"
    .replace("group_code       TEXT,\n", "group_code       TEXT CHECK ((kind='friend' AND group_code IS NULL) OR (kind='group' AND group_code IS NOT NULL)),\n")) + ENTITY_SQL

# What a normalized record may carry. ``conversation_key`` is validated against the
# target conversation and then dropped; anything else is a programming error.
RECORD_FIELDS = ("native_id_kind", "native_id", "native_seq", "sender_uin", "sender_uid",
                 "send_type", "direction", "time_ms", "msg_type", "kind", "text", "quote",
                 "status", "recall_time", "raw", "normalize_version")
BODY_FIELDS = ("kind", "text", "quote")
IDENTITY_FIELDS = ("direction", "time_ms", "sender_uin", "native_seq")
ANALYSIS_SQL = ("direction IN ('self','peer') AND status IN ('normal','revised')"
                " AND kind='text' AND qq_has_text(text)")
CHECKPOINT_FIELDS = ("cursor_version", "window_start_ms", "window_end_ms", "scanned_through_ms",
                     "last_commit_time_ms", "last_commit_count", "last_task_status", "attempts",
                     "overlap_ms", "state", "last_error", "partial_reason")


class StoreError(RuntimeError):
    pass


def message_key(account, conversation_key, native_id_kind, native_id):
    """data-schema section 2.2: computed by the application and never rewritten."""
    seed = "|".join((account, conversation_key, native_id_kind, native_id))
    return "m:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def database_path(account, root=None, profile=None):
    profile = profile or current_product()
    directory = (profile.account_directory(account) if root is None
                 else profile.account_directory(account, root))
    return directory / DATABASE_NAME


def open_store(uin, root=None, profile=None):
    """One library per account, under the product's own data root, never the WeChat one."""
    return QQMessageStore(database_path(account_key(uin), root, profile))


class QQMessageStore:
    """A single account library: one connection, every access serialized by one lock."""

    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        # Check before resolve(), which would erase evidence of a junction/symlink.
        _check_root(self.path.parent)
        _regular(self.path)
        self.path = self.path.resolve()
        self.lock = threading.RLock()
        self.closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _check_root(self.path.parent)
        self.connection = self._connect()
        with self.lock:
            try:
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 2, 3, SCHEMA_VERSION):
                    raise StoreError("schema version %s is not %s" % (version, SCHEMA_VERSION))
                if version == 2:
                    # Never modify an old library before its recoverable snapshot exists.
                    self.migration_backup = self.backup_to(self.path.with_name(
                        self.path.name + ".v2-backup-" + uuid.uuid4().hex), versions=(2,))
                if version in (0, 2):
                    with self.transaction() as cursor:
                        statements = _statements(SCHEMA_SQL if version == 0 else SCHEMA_SQL_V3)
                        for statement in statements:
                            if version == 0 or statement.startswith("CREATE TABLE message_observations"):
                                cursor.execute(statement)
                        if version == 2:
                            for row in cursor.execute("SELECT * FROM messages").fetchall():
                                _remember_observation(cursor, row["message_key"], dict(row))
                        cursor.execute("PRAGMA user_version=%d" % (SCHEMA_VERSION if version == 0 else 3))
                if version in (2, 3):
                    self._migrate_v3()
            except BaseException:
                self.connection.close()
                self.closed = True
                raise

    def _connect(self):
        connection = sqlite3.connect(str(self.path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function("qq_has_text", 1,
                                   lambda value: isinstance(value, str) and bool(value.strip()),
                                   deterministic=True)
        return connection

    def _migrate_v3(self):
        _validate_database(self.connection, (3,))
        self.migration_backup = self.backup_to(self.path.with_name(self.path.name + ".v3-backup-" + uuid.uuid4().hex), versions=(3,))
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.transaction() as cursor:
                columns = [row[1] for row in cursor.execute("PRAGMA table_info(conversations)")]
                statement = _statements(SCHEMA_SQL)[0].replace("CREATE TABLE conversations", "CREATE TABLE conversations_v4", 1)
                cursor.execute(statement)
                names = ','.join(columns)
                cursor.execute(f"INSERT INTO conversations_v4({names}) SELECT {names} FROM conversations")
                cursor.execute("DROP TABLE conversations")
                cursor.execute("ALTER TABLE conversations_v4 RENAME TO conversations")
                for statement in _statements(ENTITY_SQL):
                    cursor.execute(statement)
                cursor.execute("PRAGMA user_version=4")
                _validate_database(self.connection, (4,))
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    def assert_account(self, account):
        with self.lock:
            if self.closed:
                raise StoreError("message library is closed")
            for table in ("conversations", "messages", "identity_aliases", "import_manifest", "qq_people_v1", "qq_memberships_v1", "qq_conversation_profiles_v1"):
                if self.connection.execute(
                        f"SELECT 1 FROM {table} WHERE account_key<>? LIMIT 1", (account,)).fetchone():
                    raise StoreError("message library belongs to another account")
            from qq_sync_work import exists
            if exists(self.connection) and self.connection.execute("SELECT 1 FROM qq_sync_work_v1 WHERE account_key<>? LIMIT 1", (account,)).fetchone():
                raise StoreError("message library belongs to another account")

    def backup_to(self, destination, *, versions=(SCHEMA_VERSION,)):
        """Snapshot via SQLite's backup API; only replace the destination after validation."""
        destination = Path(os.path.abspath(destination))
        _check_root(destination.parent)
        _regular(destination)
        destination = destination.resolve()
        if destination == self.path:
            raise StoreError("backup destination is the open library")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".qq-backup-", dir=destination.parent)
        os.close(fd)
        candidate = Path(name)
        try:
            with self.lock:
                if self.closed or self.connection.in_transaction:
                    raise StoreError("library is not ready for backup")
                target = sqlite3.connect(str(candidate))
                try:
                    self.connection.backup(target)
                    _validate_database(target, versions)
                finally:
                    target.close()
            os.replace(candidate, destination)
            return destination
        finally:
            candidate.unlink(missing_ok=True)

    def restore_from(self, backup):
        """Prepare a validated replacement before closing/replacing the active file."""
        backup = Path(os.path.abspath(backup))
        _check_root(backup.parent)
        _regular(backup)
        backup = backup.resolve()
        if backup == self.path or not backup.is_file():
            raise StoreError("invalid backup file")
        fd, name = tempfile.mkstemp(prefix=".qq-restore-", dir=self.path.parent)
        os.close(fd)
        candidate = Path(name)
        try:
            source = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
            try:
                _validate_database(source, (SCHEMA_VERSION,))
                with self.lock:
                    expected_accounts = _stored_accounts(self.connection)
                if self.path.parent.parent.name == "accounts" and re.fullmatch(r"[0-9a-f]{32}", self.path.parent.name):
                    expected_accounts.add("a:" + self.path.parent.name)
                if expected_accounts and not _stored_accounts(source) <= expected_accounts:
                    raise StoreError("backup belongs to another account")
                target = sqlite3.connect(str(candidate))
                try:
                    source.backup(target)
                    _validate_database(target, (SCHEMA_VERSION,))
                finally:
                    target.close()
            finally:
                source.close()
            with self.lock:
                if self.closed or self.connection.in_transaction:
                    raise StoreError("library is not ready for restore")
                # Restoring earlier content is a new data generation, even if the
                # backup reused the same numeric revision. Old jobs/cursors must fail.
                revisions = {(row[0], row[1]): row[2] for row in self.connection.execute(
                    "SELECT account_key,conversation_key,data_revision FROM conversations")}
                with closing(sqlite3.connect(candidate)) as restored, restored:
                    from qq_sync_work import exists as work_exists
                    if work_exists(restored):
                        restored.execute("DELETE FROM qq_sync_work_v1")
                    for owner, conversation, revision in restored.execute(
                            "SELECT account_key,conversation_key,data_revision FROM conversations").fetchall():
                        newest = max(revision, revisions.get((owner, conversation), 0)) + 1
                        first = restored.execute("SELECT time_ms,local_seq FROM messages WHERE account_key=? "
                            "AND conversation_key=? ORDER BY time_ms,local_seq LIMIT 1", (owner, conversation)).fetchone()
                        restored.execute("UPDATE conversations SET data_revision=?,earliest_affected_time_ms=?,"
                            "earliest_affected_local_seq=? WHERE account_key=? AND conversation_key=?",
                            (newest, *(first or (None, None)), owner, conversation))
                _check_root(self.path.parent)
                _regular(self.path)
                self.connection.close()
                try:
                    os.replace(candidate, self.path)
                finally:
                    # If replacement failed, re-open the original unchanged library.
                    self.connection = self._connect()
            return self.path
        finally:
            candidate.unlink(missing_ok=True)

    def close(self):
        with self.lock:
            if not self.closed:
                self.connection.close()
                self.closed = True

    @contextmanager
    def transaction(self, *, commit_scope=None):
        """Messages and checkpoint commit together or vanish together (V11/R17)."""
        with self.lock:
            if self.closed:
                raise StoreError("message library is closed")
            _check_root(self.path.parent)
            cursor = self.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                with commit_scope() if commit_scope is not None else nullcontext():
                    cursor.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
            finally:
                cursor.close()

    def ensure_conversation(self, account, conversation_key, *, peer_uin=None, peer_uid=None,
                            display_name=None, kind="friend", group_code=None):
        try:
            peer_uid, group_code = conversation_identity(conversation_key, kind, peer_uid, group_code)
        except ValueError as error:
            raise StoreError(str(error)) from None
        with self.transaction() as cursor:
            self.assert_account(account)
            cursor.execute(
                "INSERT INTO conversations(conversation_key, account_key, peer_uin, peer_uid,"
                " display_name, kind, group_code) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(conversation_key) DO NOTHING",
                (conversation_key, account, peer_uin, peer_uid, display_name, kind, group_code))

    def ingest(self, account, conversation_key, records, *, checkpoint=None, now_ms=0,
               conversation=None, cancel=None, commit_scope=None, sync_work=None, receipt=None, receipt_clock=None):
        """Idempotent write of normalized records, optionally with its fetch checkpoint."""
        # Import staging supplies a disk-backed iterator. Validate within the same
        # transaction so a late invalid row or cancellation rolls back every write.
        prepared = (_checked_record(record, conversation_key) for record in records)
        if checkpoint is not None:
            unknown = set(checkpoint) - set(CHECKPOINT_FIELDS)
            if unknown:
                raise StoreError("unknown checkpoint fields: " + ",".join(sorted(unknown)))
            if "platform" in checkpoint:
                raise StoreError("checkpoint platform is fixed by the schema")
        outcome = {"inserted": 0, "unchanged": 0, "recalled": 0, "revised": 0,
                   "conflicts": 0, "backfilled": 0}
        import_conversation = conversation
        if receipt is not None:
            import qq_ingest_audit as audit
            receipt = audit.validate_receipt(receipt)
            if cancel is not None:
                cancel()
            self.ensure_ingest_audit()
        with self.transaction(commit_scope=commit_scope) as cursor:
            self.assert_account(account)
            if conversation is not None:
                kind = conversation.get("kind", "friend")
                try:
                    peer_uid, group_code = conversation_identity(conversation_key, kind, conversation.get("peerUid"), conversation.get("groupCode"))
                except ValueError as error:
                    raise StoreError(str(error)) from None
                cursor.execute("INSERT INTO conversations(conversation_key,account_key,peer_uin,peer_uid,"
                    "display_name,kind,group_code) VALUES (?,?,?,?,?,?,?) ON CONFLICT(conversation_key) DO NOTHING",
                    (conversation_key, account, conversation.get("peerUin"), peer_uid,
                     conversation.get("name"),kind,group_code))
            conversation = cursor.execute(
                "SELECT * FROM conversations WHERE account_key=? AND conversation_key=?",
                (account, conversation_key)).fetchone()
            if conversation is None:
                raise StoreError("conversation is not registered")
            if import_conversation is not None:
                if conversation['kind'] != import_conversation.get('kind','friend') or conversation['group_code'] != import_conversation.get('groupCode'):
                    raise StoreError('conversation-identity-mismatch')
                for column, key in (("peer_uin", "peerUin"), ("peer_uid", "peerUid")):
                    old, new = conversation[column], import_conversation.get(key)
                    if old and new and (canonical_uin(old) != canonical_uin(new) if column == "peer_uin" else old != new):
                        raise StoreError("peer-identity-mismatch")
                # Explicitly confirmed metadata may fill an old empty directory
                # entry, while user-edited names and known identity stay intact.
                cursor.execute("UPDATE conversations SET peer_uin=COALESCE(peer_uin,?),"
                    "peer_uid=COALESCE(peer_uid,?),display_name=COALESCE(display_name,?) "
                    "WHERE account_key=? AND conversation_key=?",
                    (import_conversation.get("peerUin"), import_conversation.get("peerUid"),
                     import_conversation.get("name"), account, conversation_key))
            covered_until = cursor.execute(
                "SELECT MAX(time_ms) FROM messages WHERE account_key=? AND conversation_key=?",
                (account, conversation_key)).fetchone()[0]
            if import_conversation and import_conversation.get('kind')=='group' and import_conversation.get('ownerUid'):
                owner_uin=import_conversation['ownerUin']
                if account_key(owner_uin)!=account:
                    raise StoreError('owner-identity-mismatch')
                upsert_person(cursor,account,uin=owner_uin,uid=import_conversation['ownerUid'],nickname=import_conversation.get('ownerName') or '')
            boundary = _current_boundary(conversation)
            touched = False
            run_id = audit.begin(cursor, account, conversation_key, receipt, now_ms) if receipt is not None else None
            seen, record_bounds = 0, None
            for record in prepared:          # source order preserved (data-schema 3)
                if cancel is not None:
                    cancel()
                if conversation['kind'] == 'group' and record.get('direction') in ('self','peer'):
                    member = upsert_person(cursor, account, uin=record.get('sender_uin'), uid=record.get('sender_uid'))
                    cursor.execute("INSERT INTO qq_memberships_v1(account_key,conversation_key,member_id) VALUES (?,?,?) "
                                   "ON CONFLICT(account_key,conversation_key,member_id) DO NOTHING", (account,conversation_key,member))
                before_outcome = {kind: outcome[kind] for kind in audit.DISPOSITIONS} if receipt is not None else None
                written = _write_record(cursor, account, conversation_key, record,
                                        covered_until, now_ms, outcome)
                if receipt is not None:
                    seen += 1
                    timestamp = record["time_ms"]
                    record_bounds = (min(record_bounds[0], timestamp), max(record_bounds[1], timestamp)) if record_bounds else (timestamp, timestamp)
                    disposition = next(kind for kind, value in before_outcome.items() if outcome[kind] > value)
                    key = message_key(account, conversation_key, record["native_id_kind"], record["native_id"])
                    audit.observe(cursor, run_id, key, _observation_fingerprint(record), record["normalize_version"], disposition,
                                  account, conversation_key, receipt["kind"])
                if written["affected"] is not None:
                    touched = True
                    candidate = written["affected"]
                    boundary = min(boundary, candidate) if boundary else candidate
            if touched or outcome["inserted"]:
                window = cursor.execute(
                    "SELECT MIN(time_ms), MAX(time_ms) FROM messages"
                    " WHERE account_key=? AND conversation_key=?",
                    (account, conversation_key)).fetchone()
                assignments = ["first_time_ms=?", "last_time_ms=?"]
                values = [window[0], window[1]]
                if touched:
                    assignments = ["data_revision=data_revision+1",
                                   "earliest_affected_time_ms=?",
                                   "earliest_affected_local_seq=?"] + assignments
                    values = [boundary[0], boundary[1]] + values
                cursor.execute("UPDATE conversations SET " + ", ".join(assignments) +
                               " WHERE account_key=? AND conversation_key=?",
                               (*values, account, conversation_key))
            if checkpoint is not None:
                _write_checkpoint(cursor, account, conversation_key, checkpoint)
            if sync_work is not None:
                from qq_sync_work import write
                write(cursor, account, conversation_key, *sync_work)
            if receipt is not None:
                revision = cursor.execute("SELECT data_revision FROM conversations WHERE account_key=? AND conversation_key=?",
                                          (account, conversation_key)).fetchone()[0]
                audit.finish(cursor, run_id, outcome, seen, record_bounds, revision,
                             receipt_clock() if receipt_clock is not None else now_ms)
            if cancel is not None:
                cancel()
        return outcome

    def set_person(self, account, **fields):
        with self.transaction() as cursor:
            self.assert_account(account)
            return upsert_person(cursor, account, **fields)

    def person_for_sender(self, account, uin=None, uid=None):
        if uin:
            uin = canonical_uin(uin)
        with self.lock:
            if uin:
                row = self.connection.execute("SELECT * FROM qq_people_v1 WHERE account_key=? AND uin=?",(account,uin)).fetchone()
            else:
                row = self.connection.execute("SELECT * FROM qq_people_v1 WHERE account_key=? AND uid=?",(account,uid)).fetchone()
            return dict(row) if row is not None else None

    def set_members(self, account, user, members, *, source='qce'):
        with self.transaction() as cursor:
            self.assert_account(account)
            for item in members:
                member = upsert_person(cursor,account,uin=item.get('uin'),uid=item.get('uid'),
                    nickname=item.get('nick') or '',avatar_url=item.get('avatarUrl') or '')
                card = item.get('cardName') or ''
                if not isinstance(card,str) or len(card)>256:
                    raise StoreError('member-card-invalid')
                cursor.execute("INSERT INTO qq_memberships_v1(account_key,conversation_key,member_id,card_name,active,source) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(account_key,conversation_key,member_id) DO UPDATE SET card_name=excluded.card_name,active=excluded.active,source=excluded.source",
                    (account,user,member,card,int(item.get('isDelete') is not True),source))

    def members(self, account, user):
        with self.lock:
            return [dict(row) for row in self.connection.execute("SELECT p.*,m.card_name,m.active,m.source "
                "FROM qq_memberships_v1 m JOIN qq_people_v1 p ON p.account_key=m.account_key AND p.member_id=m.member_id "
                "WHERE m.account_key=? AND m.conversation_key=? ORDER BY m.member_id", (account,user))]

    def target_counts(self, account, user, *, direction=None, uin=None, uid=None, highwater=None):
        clauses=['account_key=?','conversation_key=?',"direction IN ('self','peer')"]
        values=[account,user]
        if direction:
            clauses.append('direction=?');values.append(direction)
        elif uin or uid:
            clauses.append("((? IS NOT NULL AND LTRIM(sender_uin,'0')=?) OR (sender_uin IS NULL AND ? IS NOT NULL AND sender_uid=?))")
            values.extend((uin,uin,uid,uid))
        if highwater is not None:
            _check_scope(highwater,user);clauses.append('(time_ms,local_seq)<= (?,?)');values.extend((highwater[0],highwater[2]))
        with self.lock:
            row=self.connection.execute('SELECT COUNT(*),COALESCE(SUM('+ANALYSIS_SQL+'),0) FROM messages WHERE '+' AND '.join(clauses),values).fetchone()
            return tuple(row)

    def sender_counts(self,account,user):
        """Count a group's observed authors in one scan, preserving target_counts aliases."""
        with self.lock:
            rows=self.connection.execute("SELECT CASE WHEN sender_uin IS NOT NULL THEN 'uin:'||LTRIM(sender_uin,'0') "
                "ELSE 'uid:'||sender_uid END AS sender,COUNT(*) FROM messages "
                "WHERE account_key=? AND conversation_key=? AND direction IN ('self','peer') GROUP BY sender",
                (account,user)).fetchall()
        return {sender:count for sender,count in rows if sender is not None}

    def membership(self,account,user,member_id):
        with self.lock:
            row=self.connection.execute('SELECT card_name FROM qq_memberships_v1 '
                'WHERE account_key=? AND conversation_key=? AND member_id=?',(account,user,member_id)).fetchone()
        return dict(row) if row else None

    def set_conversation_profile(self, account, user, *, name='', avatar_url='', source_version=None):
        if not isinstance(name,str) or len(name)>256:
            raise StoreError('conversation-name-invalid')
        with self.transaction() as cursor:
            self.assert_account(account)
            cursor.execute("INSERT INTO qq_conversation_profiles_v1(account_key,conversation_key,name,avatar_url,source_version) VALUES (?,?,?,?,?) "
                "ON CONFLICT(account_key,conversation_key) DO UPDATE SET name=excluded.name,avatar_url=excluded.avatar_url,source_version=excluded.source_version",
                (account,user,name,avatar(avatar_url),source_version))

    def conversation_profile(self, account, user):
        with self.lock:
            row = self.connection.execute("SELECT * FROM qq_conversation_profiles_v1 WHERE account_key=? AND conversation_key=?",(account,user)).fetchone()
            return dict(row) if row is not None else None

    def ensure_ingest_audit(self):
        from qq_ingest_audit import exists, validate_table, DDL, needs_format_upgrade, upgrade_formats
        with self.lock:
            if exists(self.connection):
                validate_table(self.connection)
                if needs_format_upgrade(self.connection):
                    self.backup_to(self.path.with_name(self.path.name + '.json-import-backup-' + uuid.uuid4().hex))
                    with self.transaction() as cursor:
                        upgrade_formats(cursor)
                return
            self.backup_to(self.path.with_name(self.path.name + ".ingest-audit-backup-" + uuid.uuid4().hex))
            with self.transaction() as cursor:
                for statement in DDL:
                    cursor.execute(statement)

    def checkpoint(self, account, conversation_key):
        with self.lock:
            self.assert_account(account)
            row = self.connection.execute("SELECT * FROM sync_checkpoints WHERE account_key=? AND conversation_key=?",
                                          (account, conversation_key)).fetchone()
            return dict(row) if row is not None else None

    def ensure_sync_work(self):
        from qq_sync_work import exists, validate_table, DDL
        with self.lock:
            if exists(self.connection):
                validate_table(self.connection)
                return
            self.backup_to(self.path.with_name(self.path.name + ".sync-work-backup-" + uuid.uuid4().hex))
            with self.transaction() as cursor:
                cursor.execute(DDL)

    def sync_work(self, account, conversation_key, kind):
        from qq_sync_work import exists, validate_table, validate
        with self.lock:
            self.assert_account(account)
            if not exists(self.connection):
                return None
            validate_table(self.connection)
            row = self.connection.execute("SELECT revision,payload FROM qq_sync_work_v1 "
                "WHERE account_key=? AND conversation_key=? AND kind=?", (account, conversation_key, kind)).fetchone()
            return {"revision": row[0], "payload": validate(kind, json.loads(row[1]))} if row is not None else None

    def list_conversations(self, account):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM conversations WHERE account_key=? ORDER BY"
                " COALESCE(last_time_ms, 0) DESC, conversation_key", (account,)).fetchall()

    def latest(self, account, conversation_key, limit, offset=0):
        """The newest `limit` rows, oldest-first, plus whether anything older is still there."""
        if limit < 1:
            raise ValueError("invalid limit")
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM messages WHERE account_key=? AND conversation_key=?"
                " ORDER BY time_ms DESC, local_seq DESC LIMIT ? OFFSET ?",
                (account, conversation_key, limit, offset)).fetchall()
            total = self.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE account_key=? AND conversation_key=?",
                (account, conversation_key)).fetchone()[0]
        return list(reversed(rows)), offset + len(rows) < total

    def counts(self, account, conversation_key, highwater=None, *, peer_only=False):
        suffix, values = "", [account, conversation_key]
        if highwater is not None:
            _check_scope(highwater, conversation_key)
            suffix = " AND (time_ms, local_seq) <= (?,?)"
            values += [highwater[0], highwater[2]]
        if peer_only:
            suffix += " AND direction='peer'"
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(direction='peer' AND " + ANALYSIS_SQL +
                "), 0) AS text FROM messages"
                " WHERE account_key=? AND conversation_key=?" + suffix, values).fetchone()
        # Only the peer's own writing counts toward a portrait (V18); never our own rows.
        return row["total"], row["text"]

    def before(self, account, conversation_key, position, limit=3):
        """Rows strictly earlier than a canonical ``(time_ms, scope, local_seq)`` position."""
        _check_scope(position, conversation_key)
        time_ms, local_seq = position[0], position[2]
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM messages WHERE account_key=? AND conversation_key=?"
                " AND (time_ms, local_seq) < (?,?) AND " + ANALYSIS_SQL +
                " ORDER BY time_ms DESC, local_seq DESC"
                " LIMIT ?", (account, conversation_key, time_ms, local_seq, limit)).fetchall()
        return list(reversed(rows))

    def coverage(self, account, conversation_key):
        """Single-conversation coverage, kept separate from the account readiness gate."""
        with self.lock:
            checkpoint = self.connection.execute(
                "SELECT * FROM sync_checkpoints WHERE account_key=? AND conversation_key=?",
                (account, conversation_key)).fetchone()
        covered = checkpoint is not None and checkpoint["last_task_status"] in (
            "complete", "complete-empty")
        return {"conversationKey": conversation_key, "covered": covered,
                "state": checkpoint["state"] if checkpoint else None,
                "scannedThroughMs": checkpoint["scanned_through_ms"] if checkpoint else 0,
                "lastTaskStatus": checkpoint["last_task_status"] if checkpoint else None,
                "partialReason": checkpoint["partial_reason"] if checkpoint else None,
                "attempts": checkpoint["attempts"] if checkpoint else 0}

    def highwater(self, account, conversation_key):
        """``(time_ms, 'qq:'||conversation_key, local_seq)`` or None for an empty chat."""
        with self.lock:
            row = self.connection.execute(
                "SELECT time_ms, local_seq FROM messages WHERE account_key=? AND"
                " conversation_key=? ORDER BY time_ms DESC, local_seq DESC LIMIT 1",
                (account, conversation_key)).fetchone()
        return None if row is None else (row["time_ms"], "qq:" + conversation_key,
                                         row["local_seq"])

    def page(self, account, conversation_key, after=None, page_size=256, *,
             highwater=None, quoted_only=False):
        """Canonical-order paging; a non-empty page always returns its last cursor (C02-R3)."""
        if page_size < 1:
            raise ValueError("invalid page size")
        where = ["account_key=?", "conversation_key=?"]
        values = [account, conversation_key]
        if after is not None:
            _check_scope(after, conversation_key)
            where.append("(time_ms, local_seq) > (?,?)")
            values += [int(after[0]), int(after[2])]
        if highwater is not None:
            _check_scope(highwater, conversation_key)
            where.append("(time_ms, local_seq) <= (?,?)")
            values += [highwater[0], highwater[2]]
        if quoted_only:
            where.append(ANALYSIS_SQL + " AND qq_has_text(quote)")
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM messages WHERE " + " AND ".join(where) +
                " ORDER BY time_ms ASC, local_seq ASC LIMIT ?", values + [page_size]).fetchall()
        if not rows:
            return [], None
        last = rows[-1]
        return rows, (last["time_ms"], "qq:" + conversation_key, last["local_seq"])

    def texts_for_refs(self, account, conversation_key, refs, with_ids=False):
        """Missing references are skipped rather than raising (source contract 2.2)."""
        found = []
        with self.lock:
            for scope, local_seq, native_id in refs:
                _check_scope((0, scope, local_seq), conversation_key)
                row = self.connection.execute(
                    "SELECT text, native_id FROM messages WHERE account_key=?"
                    " AND conversation_key=? AND local_seq=? AND native_id=? AND " + ANALYSIS_SQL,
                    (account, conversation_key, local_seq, native_id)).fetchone()
                if row is not None and row["text"] is not None:
                    found.append((row["native_id"], row["text"]) if with_ids else row["text"])
        return found

    def record_alias(self, account, *, uin, uid="", peer_uid="", alias_kind, evidence,
                     confidence, created_at):
        if confidence not in ("confirmed", "probable", "pending"):
            raise ValueError("invalid alias confidence")
        with self.transaction() as cursor:
            cursor.execute(
                "INSERT INTO identity_aliases(account_key, uin, uid, peer_uid, alias_kind,"
                " evidence, confidence, created_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_key, uin, uid, peer_uid, alias_kind) DO NOTHING",
                (account, uin, uid, peer_uid, alias_kind, evidence, confidence, created_at))

    def revision(self, account, conversation_key):
        with self.lock:
            row = self.connection.execute(
                "SELECT data_revision, earliest_affected_time_ms, earliest_affected_local_seq"
                " FROM conversations WHERE account_key=? AND conversation_key=?",
                (account, conversation_key)).fetchone()
        return None if row is None else (row["data_revision"], row["earliest_affected_time_ms"],
                                         row["earliest_affected_local_seq"])

    def integrity_errors(self):
        with self.lock:
            return self.connection.execute("PRAGMA foreign_key_check").fetchall()


def _statements(script):
    return [part.strip() for part in script.split(";") if part.strip()]


def _check_scope(cursor_tuple, conversation_key):
    if (not isinstance(cursor_tuple, (tuple, list)) or len(cursor_tuple) != 3 or
            cursor_tuple[1] != "qq:" + conversation_key or
            type(cursor_tuple[0]) is not int or type(cursor_tuple[2]) is not int or
            not 0 <= cursor_tuple[0] <= 2**63 - 1 or not 0 <= cursor_tuple[2] <= 2**63 - 1):
        raise ValueError("invalid cursor scope")
    return cursor_tuple


def _current_boundary(conversation):
    time_ms, local_seq = (conversation["earliest_affected_time_ms"],
                          conversation["earliest_affected_local_seq"])
    return None if time_ms is None or local_seq is None else (int(time_ms), int(local_seq))


def _checked_record(record, conversation_key):
    unexpected = set(record) - set(RECORD_FIELDS) - {"conversation_key"}
    if unexpected:
        raise StoreError("unknown record fields: " + ",".join(sorted(unexpected)))
    if record.get("conversation_key", conversation_key) != conversation_key:
        raise StoreError("record belongs to another conversation")
    if not isinstance(record.get("native_seq"), (str, type(None))):
        raise StoreError("native_seq must stay text")
    return record


def _write_record(cursor, account, conversation_key, record, covered_until, now_ms, outcome):
    key = message_key(account, conversation_key, record["native_id_kind"], record["native_id"])
    existing = cursor.execute(
        "SELECT * FROM messages WHERE account_key=? AND conversation_key=?"
        " AND native_id_kind=? AND native_id=?",
        (account, conversation_key, record["native_id_kind"], record["native_id"])).fetchone()
    is_backfill = covered_until is not None and record["time_ms"] <= covered_until
    if existing is None:
        local_seq = (cursor.execute(
            "SELECT MAX(local_seq) FROM messages WHERE account_key=? AND conversation_key=?",
            (account, conversation_key)).fetchone()[0] or 0) + 1
        columns = ["message_key", "account_key", "conversation_key", "local_seq", *RECORD_FIELDS]
        values = [key, account, conversation_key, local_seq,
                  *[record.get(name) for name in RECORD_FIELDS]]
        cursor.execute("INSERT INTO messages(" + ",".join(columns) + ") VALUES (" +
                       ",".join("?" * len(columns)) + ")", values)
        outcome["inserted"] += 1
        if is_backfill:
            # Anything older than what was already stored can change earlier conclusions
            # (X5); a pure append cannot.
            outcome["backfilled"] += 1
        _remember_observation(cursor, key, record)
        return {"local_seq": local_seq,
                "affected": (record["time_ms"], local_seq) if is_backfill else None}
    if not _remember_observation(cursor, key, record):
        outcome["unchanged"] += 1
        return {"local_seq": existing["local_seq"], "affected": None}
    changed = [name for name in BODY_FIELDS + IDENTITY_FIELDS
               if _identity_value(name, existing[name]) != _identity_value(name, record[name])
               and not (name == "native_seq" and (existing[name] is None or record[name] is None))]
    if not changed and existing["native_seq"] is None and record.get("native_seq") is not None:
        cursor.execute("UPDATE messages SET native_seq=? WHERE message_key=?", (record["native_seq"], key))
    incoming_recall = record["status"] == "recalled"
    recalled = existing["status"] == "recalled" or incoming_recall
    conflicted = bool(changed) or record["status"] == "conflict" or existing["status"] == "conflict"
    status = "recalled" if recalled else "conflict" if conflicted else existing["status"]
    recall_time = record.get("recall_time") if incoming_recall else existing["recall_time"]
    if not changed and status == existing["status"] and recall_time == existing["recall_time"]:
        outcome["unchanged"] += 1
        return {"local_seq": existing["local_seq"], "affected": None}
    # Keep the canonical body's raw evidence intact. Every novel competing version
    # (including a recall) is saved separately and fingerprinted for stable replay.
    _record_conflict(cursor, account, conversation_key, record, existing, now_ms)
    cursor.execute("UPDATE messages SET status=?, recall_time=?, revision=revision+1"
                   " WHERE message_key=?", (status, recall_time, key))
    if incoming_recall and existing["status"] != "recalled":
        outcome["recalled"] += 1
    else:
        outcome["conflicts"] += 1
    if is_backfill:
        outcome["backfilled"] += 1
    return {"local_seq": existing["local_seq"],
            "affected": min((existing["time_ms"], existing["local_seq"]),
                            (record["time_ms"], existing["local_seq"]))}


def _observation_fingerprint(record):
    semantic = {name: _identity_value(name, record.get(name)) for name in
                BODY_FIELDS + IDENTITY_FIELDS + ("status", "recall_time")}
    if record.get("status") != "recalled":
        semantic["recall_time"] = None
    return hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _remember_observation(cursor, key, record):
    fingerprint = _observation_fingerprint(record)
    cursor.execute("INSERT OR IGNORE INTO message_observations(message_key,fingerprint) VALUES (?,?)",
                   (key, fingerprint))
    return cursor.rowcount == 1


def _identity_value(name, value):
    if name == "sender_uin" and isinstance(value, str) and value.isascii() and value.isdecimal():
        return value.lstrip("0") or "0"
    return value


def _validate_database(connection, versions):
    from qq_ingest_audit import validate_database as validate_ingest_audit
    try:
        validate_ingest_audit(connection)
    except (ValueError, TypeError):
        raise StoreError("backup ingest audit is invalid") from None
    from qq_sync_work import exists as work_exists, validate_table, validate
    if work_exists(connection):
        try:
            validate_table(connection)
            for kind, payload in connection.execute("SELECT kind,payload FROM qq_sync_work_v1"):
                validate(kind, json.loads(payload))
        except (ValueError, TypeError):
            raise StoreError("backup sync work is invalid") from None
    if connection.execute("PRAGMA user_version").fetchone()[0] not in versions:
        raise StoreError("unsupported backup schema")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise StoreError("backup integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise StoreError("backup foreign key check failed")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"conversations", "messages", "message_conflicts", "sync_checkpoints",
                "identity_aliases", "import_manifest"}
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version >= 3:
        required.add("message_observations")
    if version >= 4:
        required.update(("qq_people_v1", "qq_memberships_v1", "qq_conversation_profiles_v1"))
    if not required <= tables:
        raise StoreError("backup schema is incomplete")
    def normalized(sql):
        sql = re.sub(r'"(\w+)"', r'\1', sql or '')
        return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql)).strip().rstrip(";").casefold()
    actual = {row[0]: normalized(row[1]) for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index')")}
    for statement in _statements(SCHEMA_SQL if version >= 4 else SCHEMA_SQL_V3):
        match = re.match(r"CREATE (?:TABLE|INDEX) (\w+)", statement)
        if match and (match[1] != "message_observations" or "message_observations" in required):
            if actual.get(match[1]) != normalized(statement):
                raise StoreError("backup schema differs from the supported layout")


def _stored_accounts(connection):
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {row[0] for table in ("conversations", "messages", "identity_aliases", "import_manifest", "qq_people_v1", "qq_memberships_v1", "qq_conversation_profiles_v1") if table in tables
            for row in connection.execute(f"SELECT DISTINCT account_key FROM {table}")}


def _record_conflict(cursor, account, conversation_key, record, existing, now_ms):
    """Both sides stay on disk: the old row keeps its body, the new one is kept as evidence."""
    cursor.execute(
        "INSERT INTO message_conflicts(account_key, conversation_key, native_id_kind,"
        " native_id, existing_revision, incoming_raw, detected_at) VALUES (?,?,?,?,?,?,?)",
        (account, conversation_key, record["native_id_kind"], record["native_id"],
         existing["revision"], record.get("raw"), now_ms))


def _write_checkpoint(cursor, account, conversation_key, checkpoint):
    columns = ["account_key", "conversation_key", *CHECKPOINT_FIELDS]
    values = [account, conversation_key, *[checkpoint.get(name) for name in CHECKPOINT_FIELDS]]
    assignments = ", ".join("%s=excluded.%s" % (name, name) for name in CHECKPOINT_FIELDS)
    # The schema pins platform to 'qq'; callers cannot choose it (source contract 4.1).
    cursor.execute(
        "INSERT INTO sync_checkpoints(" + ",".join(columns) + ", platform) VALUES (" +
        ",".join("?" * len(columns)) + ",'qq') ON CONFLICT(account_key, conversation_key)"
        " DO UPDATE SET " + assignments, values)

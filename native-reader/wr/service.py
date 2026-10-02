"""Native reader service: method dispatch and state machine.

State values match the frozen contract. Unknown/key failures are reported truthfully. Live mode
decrypts each message shard in memory with a per-salt PageKey, binds the account's real username
from the storage directory name cross-validated against Name2Id, and freezes ciphertext snapshots
in a shared per-account dataset (no plaintext ever persisted outside explicit fixture mode).

The reader never writes to WeChat, never scans unrelated processes, and keeps every key in memory.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field

from . import crypto, database, discovery, errors, fixture, protocol, snapshot, wal, window

PAGE_SIZE = crypto.PAGE_SIZE
SALT_SIZE = crypto.SALT_SIZE
MAX_SNAPSHOT_CACHE = 8
MAX_DATASETS = 16
INSTANCE_SUFFIX = re.compile(r"^(.+)_([0-9a-fA-F]{2,16})$")


@dataclass
class AccountState:
    id: str
    name: str
    state: str
    message: str = ""
    username: str | None = None
    dir: discovery.AccountDir | None = None
    shards: list = field(default_factory=list)
    page_key: crypto.PageKey | None = None
    keys_by_salt: dict[bytes, crypto.PageKey] = field(default_factory=dict)
    contact_meta: dict = field(default_factory=dict)
    fixture_account: fixture.FixtureAccount | None = None


def _map_kind(kind: str) -> str:
    if kind in ("friend", "private", "chatroom"):
        return "friend" if kind != "chatroom" else "group"
    if kind in ("group", "service", "unknown"):
        return kind
    return "unknown"


def _int_param(value: object, default: int, low: int, high: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise errors.ProtocolError(errors.BAD_REQUEST, f"{name} must be an integer") from exc
    if number < low or number > high:
        raise errors.ProtocolError(errors.BAD_REQUEST, f"{name} must be {low}..{high}")
    return number


def _classify_shard(path: str) -> str:
    lowered = path.replace("\\", "/").lower()
    name = os.path.basename(lowered)
    if "/message/" in lowered or name.startswith("message"):
        return "message"
    if "/contact/" in lowered or "contact" in name:
        return "contact"
    if "/session/" in lowered or "session" in name:
        return "session"
    return "other"


def _account_username_candidates(dir_path: str) -> list[str]:
    """Candidate self usernames from the account directory name (instance suffix stripped).

    The account directory is `<username>_<instance>` (e.g. ``wxid_ab12``); the exact username is
    only accepted after cross-validating it against a shard's real Name2Id mapping.
    """
    parent = os.path.basename(os.path.dirname(os.path.abspath(dir_path)))
    candidates: list[str] = []
    if parent:
        candidates.append(parent)
        match = INSTANCE_SUFFIX.match(parent)
        if match and match.group(1):
            candidates.append(match.group(1))
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _read_stable(path: str, retries: int = 2) -> bytes:
    """Read a file only if its identity (size, mtime) is unchanged across the read."""
    for _ in range(retries + 1):
        try:
            before = os.stat(path)
            with open(path, "rb") as handle:
                data = handle.read()
            after = os.stat(path)
        except OSError as exc:
            raise errors.ProtocolError(errors.READ_ERROR, "source unreadable") from exc
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and len(
            data
        ) == after.st_size:
            return data
    raise errors.ProtocolError(errors.READ_ERROR, "source changed during snapshot")


def _read_first_page(path: str) -> bytes | None:
    for _ in range(3):
        try:
            before = os.stat(path)
            with open(path, "rb") as handle:
                page = handle.read(PAGE_SIZE)
            after = os.stat(path)
        except OSError:
            return None
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ) and len(page) == PAGE_SIZE:
            return page
    return None


def _file_fingerprint(path: str | None) -> tuple:
    if not path:
        return ("", -1, -1)
    try:
        stat = os.stat(path)
        return (path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (path, None, None)


def _decrypt_shard_image(db: bytes, wal_data: bytes | None, page_key: crypto.PageKey) -> bytes:
    """Decrypt a whole SQLCipher shard (main DB + committed WAL) into an in-memory image."""
    if not db or len(db) % PAGE_SIZE != 0:
        raise errors.ProtocolError(errors.READ_ERROR, "shard size is not page aligned")
    pages: dict[int, bytes] = {}
    for offset in range(0, len(db), PAGE_SIZE):
        page = db[offset : offset + PAGE_SIZE]
        page_number = offset // PAGE_SIZE + 1
        pages[page_number] = crypto.decrypt_page(page, page_key, page_number, page_number == 1)
    commit_pages: int | None = None
    if wal_data:
        parsed = wal.parse_wal(wal_data)
        if parsed.corrupt_before_commit:
            raise errors.ProtocolError(errors.READ_ERROR, "wal corrupt before commit")
        for frame in parsed.frames:
            # SQLCipher keeps page 1's 16-byte salt inside the WAL page-1 frame too.
            pages[frame.page_number] = crypto.decrypt_page(
                frame.page, page_key, frame.page_number, frame.page_number == 1
            )
        commit_pages = parsed.commit_db_size
    return database.sqlite_image_from_pages(pages, commit_pages=commit_pages)


class NativeService:
    def __init__(self, cache_dir: str, fixture_manifest: str | None = None) -> None:
        self.store = snapshot.SnapshotStore(cache_dir)
        self.mode = "fixture" if fixture_manifest else "live"
        self.fixture_manifest = fixture_manifest
        self._fixture_accounts: list[fixture.FixtureAccount] = (
            fixture.load_fixture(fixture_manifest) if fixture_manifest else []
        )
        self.state = "disconnected"
        self.message = ""
        self.accounts: dict[str, AccountState] = {}
        self.account_id: str | None = None
        self.store_handle: database.ContactStore | None = None
        self._snapshots: "OrderedDict[str, dict]" = OrderedDict()
        self._processes: list[discovery.WeixinProcess] = []
        self._store_fingerprint: tuple | None = None
        if self.mode == "fixture":
            for account in self._fixture_accounts:
                self.accounts[account.id] = AccountState(
                    id=account.id,
                    name=account.name,
                    state="available",
                    username=account.username,
                    fixture_account=account,
                )
            self.state = "select_account"
            self.message = "请选择账号"

    # -- helpers -----------------------------------------------------------

    def _account_status(self) -> dict:
        accounts = []
        for account in self.accounts.values():
            entry = {"id": account.id, "name": account.name, "state": account.state}
            if account.message:
                entry["message"] = account.message
            accounts.append(entry)
        return {"state": self.state, "message": self.message, "accounts": accounts, "accountId": self.account_id}

    def _require_ready(self) -> None:
        if self.state not in ("ready", "paused") or self.store_handle is None or self.account_id is None:
            raise errors.ProtocolError(errors.NOT_CONNECTED, "not connected")

    def _self_username(self) -> str | None:
        if self.account_id and self.account_id in self.accounts:
            return self.accounts[self.account_id].username
        return None

    def _remember_snapshot(self, snapshot_id: str, record: dict) -> None:
        existing = self._snapshots.pop(snapshot_id, None)
        if existing is not None and existing is not record:
            existing["store"].close()
        self._snapshots[snapshot_id] = record
        while len(self._snapshots) > MAX_SNAPSHOT_CACHE:
            _, old = self._snapshots.popitem(last=False)
            old["store"].close()

    # -- methods -----------------------------------------------------------

    def hello(self) -> dict:
        return {"protocolVersion": protocol.PROTOCOL_VERSION, "mode": self.mode}

    def status(self) -> dict:
        if self.mode == "live" and not self.accounts:
            self._refresh_live_accounts()
        return self._account_status()

    def accounts_method(self) -> list[dict]:
        if self.mode == "live" and not self.accounts:
            self._refresh_live_accounts()
        return [
            {"id": account.id, "name": account.name, "state": account.state, **({"message": account.message} if account.message else {})}
            for account in self.accounts.values()
        ]

    def _refresh_live_accounts(self) -> None:
        self._processes = discovery.find_weixin_processes()
        if not self._processes:
            self.state = "wechat_closed"
            self.message = "Weixin.exe 未运行"
            return
        dirs = discovery.discover_account_dirs()
        active = discovery.active_account_id(dirs, self._processes)
        for account_dir in dirs:
            opaque = account_dir.id
            label = "当前登录账号" if active == opaque else opaque
            self.accounts[opaque] = AccountState(id=opaque, name=label, state="available", dir=account_dir)
        self.state = "select_account" if self.accounts else "not_logged_in"
        self.message = "请选择账号" if self.accounts else "未发现本地账号目录"

    def connect(self, params: dict) -> dict:
        return self._connect_fixture(params) if self.mode == "fixture" else self._connect_live(params)

    # -- fixture mode ------------------------------------------------------

    def _build_fixture_store(self, account: AccountState) -> database.ContactStore:
        spec = account.fixture_account
        assert spec is not None
        shards = [database.SqliteShard(shard.id, shard.image, account.id, account.username) for shard in spec.shards]
        contact_ids = {contact.username: contact.id for contact in spec.contacts}
        contact_meta = {contact.username: {"name": contact.name, "kind": _map_kind(contact.kind)} for contact in spec.contacts}
        return database.ContactStore(account.id, shards, contact_ids, contact_meta)

    def _connect_fixture(self, params: dict) -> dict:
        requested = params.get("accountId")
        if requested is None:
            if len(self.accounts) != 1:
                raise errors.ProtocolError(errors.SELECT_ACCOUNT, "multiple accounts require explicit selection")
            requested = next(iter(self.accounts))
        account = self.accounts.get(str(requested))
        if account is None:
            raise errors.ProtocolError(errors.NOT_FOUND, "unknown account")
        account.shards = []
        self.store_handle = self._build_fixture_store(account)
        self.account_id = account.id
        self.state = "ready"
        self.message = "fixture ready"
        return self._account_status()

    # -- live mode ---------------------------------------------------------

    def _connect_live(self, params: dict) -> dict:
        requested = params.get("accountId")
        if not self.accounts:
            self._refresh_live_accounts()
        if not self._processes:
            self.state = "wechat_closed"
            self.message = "Weixin.exe 未运行"
            return self._account_status()
        if requested is None:
            if len(self.accounts) != 1:
                self.state = "select_account"
                self.message = "请选择账号"
                return self._account_status()
            requested = next(iter(self.accounts))
        account = self.accounts.get(str(requested))
        if account is None:
            raise errors.ProtocolError(errors.NOT_FOUND, "unknown account")
        if account.dir is None:
            raise errors.ProtocolError(errors.NOT_FOUND, "account dir missing")

        shards = discovery.discover_shards(account.dir)
        message_shards = [shard for shard in shards if _classify_shard(shard.path) == "message"]
        meta_shards = [shard for shard in shards if _classify_shard(shard.path) in ("contact", "session")]
        if not message_shards:
            self.state = "not_logged_in"
            self.message = "该账号无可读消息分片"
            return self._account_status()

        page_ones: dict[bytes, bytes] = {}
        message_salts: set[bytes] = set()
        for shard in message_shards:
            page = _read_first_page(shard.path)
            if page is None:
                self.state = "permission_denied"
                self.message = "无法读取分片头"
                return self._account_status()
            message_salts.add(page[:SALT_SIZE])
            page_ones.setdefault(page[:SALT_SIZE], page)
        for shard in meta_shards:
            page = _read_first_page(shard.path)
            if page is not None:
                page_ones.setdefault(page[:SALT_SIZE], page)
        keys = self._resolve_keys(page_ones, message_salts)
        if not message_salts.issubset(keys):
            self.state = "error"
            self.message = "无法获取数据库密钥（需在已登录状态下重试）"
            return self._account_status()
        account.keys_by_salt = keys

        try:
            handles = []
            for shard in message_shards:
                page = _read_first_page(shard.path)
                if page is None:
                    raise errors.ProtocolError(errors.READ_ERROR, "message shard page 1 unreadable")
                page_key = keys.get(page[:SALT_SIZE])
                if page_key is None:
                    raise errors.ProtocolError(errors.KEY_UNAVAILABLE, "no key for message shard")
                data = _read_stable(shard.path)
                wal_data = _read_stable(shard.wal_path) if shard.wal_path else None
                image = _decrypt_shard_image(data, wal_data, page_key)
                handles.append(database.SqliteShard(shard.id, image, account.id, None))
        except errors.ProtocolError as exc:
            self.state = "error"
            self.message = exc.message
            return self._account_status()

        self_username = self._resolve_self_username(account.dir.path, handles)
        for handle in handles:
            handle.set_self_username(self_username)
        account.username = self_username
        account.contact_meta = self._read_contact_meta(meta_shards, keys)
        account.shards = message_shards
        account.state = "connected"
        self.store_handle = database.ContactStore(account.id, handles, None, account.contact_meta)
        self.account_id = account.id
        self._store_fingerprint = self._source_fingerprint()
        self.state = "ready"
        self.message = "ready" if self_username else "ready (self identity unresolved)"
        return self._account_status()

    def _resolve_keys(
        self, page_ones: dict[bytes, bytes], required: set[bytes]
    ) -> dict[bytes, crypto.PageKey]:
        """Validate every candidate against its matching real page-1 ciphertext.

        All required salts must resolve; a single found key never short-circuits the rest.
        """
        keys: dict[bytes, crypto.PageKey] = {}
        if not page_ones:
            return keys
        for process in self._processes:
            try:
                candidates = discovery.scan_key_candidates(process.pid, set(page_ones), page_ones)
            except errors.ProtocolError:
                continue
            for candidate in candidates:
                if candidate.salt not in page_ones or candidate.salt in keys:
                    continue
                page_key = crypto.validate_key(candidate.key, candidate.salt, page_ones[candidate.salt])
                if page_key is not None:
                    keys[candidate.salt] = page_key
            if required.issubset(keys):
                break
        return keys

    def _resolve_self_username(self, dir_path: str, handles: list[database.SqliteShard]) -> str | None:
        candidates = _account_username_candidates(dir_path)
        if not candidates:
            return None
        known: set[str] = set()
        for handle in handles:
            known.update(handle.known_usernames())
        present = [candidate for candidate in candidates if candidate in known]
        # Unique cross-validation only: ambiguous/missing stays unresolved (never guessed).
        return present[0] if len(present) == 1 else None

    def _read_contact_meta(self, meta_shards: list, keys: dict[bytes, crypto.PageKey]) -> dict:
        meta: dict[str, dict] = {}
        for shard in meta_shards:
            page = _read_first_page(shard.path)
            if page is None:
                continue
            page_key = keys.get(page[:SALT_SIZE])
            if page_key is None:
                continue
            try:
                data = _read_stable(shard.path)
                wal_data = _read_stable(shard.wal_path) if shard.wal_path else None
                image = _decrypt_shard_image(data, wal_data, page_key)
                handle = database.SqliteShard(shard.id, image, "metadata", None)
            except errors.ProtocolError:
                continue
            try:
                for username, entry in handle.contact_metadata().items():
                    meta.setdefault(username, {}).update(entry)
            finally:
                handle.close()
        return meta

    def _source_fingerprint(self) -> tuple | None:
        account = self.accounts.get(self.account_id) if self.account_id else None
        if account is None or not account.shards:
            return None
        parts = []
        for shard in account.shards:
            parts.append(_file_fingerprint(shard.path))
            parts.append(_file_fingerprint(shard.wal_path))
        return tuple(parts)

    def _refresh_live_store_if_stale(self) -> None:
        """Rebuild the live store from source only when a message shard changed (bounded)."""
        if self.mode != "live":
            return
        account = self.accounts.get(self.account_id) if self.account_id else None
        if account is None or not account.shards:
            return
        fingerprint = self._source_fingerprint()
        if fingerprint == self._store_fingerprint and self.store_handle is not None:
            return
        handles: list[database.SqliteShard] = []
        try:
            for shard in account.shards:
                page = _read_first_page(shard.path)
                if page is None:
                    raise errors.ProtocolError(errors.READ_ERROR, "message shard page 1 unreadable")
                page_key = account.keys_by_salt.get(page[:SALT_SIZE])
                if page_key is None:
                    raise errors.ProtocolError(errors.KEY_UNAVAILABLE, "no key for message shard")
                data = _read_stable(shard.path)
                wal_data = _read_stable(shard.wal_path) if shard.wal_path else None
                image = _decrypt_shard_image(data, wal_data, page_key)
                handles.append(database.SqliteShard(shard.id, image, account.id, account.username))
        except errors.ProtocolError as exc:
            for handle in handles:
                handle.close()
            self.state = "error"
            self.message = exc.message
            raise
        old = self.store_handle
        self.store_handle = database.ContactStore(account.id, handles, None, account.contact_meta)
        self._store_fingerprint = fingerprint
        if old is not None:
            old.close()

    def contacts(self, params: dict) -> list[dict]:
        self._require_ready()
        account_id = params.get("accountId")
        if account_id is not None and account_id != self.account_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "account mismatch")
        return self.store_handle.contacts()

    # -- snapshots ---------------------------------------------------------

    def _dataset_id(self, cutoff: int, fingerprint: tuple) -> str:
        basis = f"{self.account_id}\x00{cutoff}\x00{self.mode}\x00{repr(fingerprint)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def _capture_dataset(self, cutoff: int) -> str:
        """Freeze one ciphertext dataset for (account, cutoff, source fingerprint)."""
        if self.mode == "fixture":
            blobs = [(shard.shard_id, shard.image) for shard in self.store_handle.shards]
            fingerprint = tuple((sid, hashlib.sha256(blob).hexdigest()) for sid, blob in blobs)
            dataset_id = self._dataset_id(cutoff, fingerprint)
            shard_meta = [{"shardId": sid, "hasWal": False} for sid, _ in blobs]
            stored = [(sid + "#db", blob) for sid, blob in blobs]
        else:
            account = self.accounts[self.account_id]  # type: ignore[index]
            stored = []
            shard_meta = []
            parts = []
            for shard in account.shards:
                data = _read_stable(shard.path)
                stored.append((shard.id + "#db", data))
                has_wal = False
                if shard.wal_path:
                    wal_data = _read_stable(shard.wal_path)
                    if wal_data:
                        stored.append((shard.id + "#wal", wal_data))
                        has_wal = True
                shard_meta.append({"shardId": shard.id, "hasWal": has_wal})
                parts.append((shard.id, len(data), hashlib.sha256(data).hexdigest()))
            fingerprint = tuple(parts)
            dataset_id = self._dataset_id(cutoff, fingerprint)
        meta = {
            "accountId": self.account_id,
            "mode": self.mode,
            "cutoffTime": cutoff,
            "shards": shard_meta,
        }
        if not self.store.has_dataset(dataset_id):
            self.store.create_dataset(dataset_id, meta, stored)
            self.store.prune_datasets(MAX_DATASETS)
        return dataset_id

    def _record_from_meta(self, meta: dict) -> dict:
        dataset_id = meta["datasetId"]
        dataset_meta = self.store.load_dataset_meta(dataset_id)
        account = self.accounts.get(str(meta["accountId"]))
        keys = account.keys_by_salt if account is not None else {}
        self_username = meta.get("selfUsername")
        shards: list[database.SqliteShard] = []
        for shard_meta in dataset_meta.get("shards", []):
            shard_id = str(shard_meta["shardId"])
            blob = self.store.load_dataset_blob(dataset_id, shard_id + "#db")
            if meta.get("mode") == "fixture":
                image = blob
            else:
                if len(blob) < SALT_SIZE:
                    raise errors.ProtocolError(errors.SNAPSHOT_ERROR, "dataset blob too small")
                page_key = keys.get(blob[:SALT_SIZE])
                if page_key is None:
                    raise errors.ProtocolError(errors.KEY_UNAVAILABLE, "snapshot key unavailable")
                wal_blob = (
                    self.store.load_dataset_blob(dataset_id, shard_id + "#wal")
                    if shard_meta.get("hasWal")
                    else None
                )
                image = _decrypt_shard_image(blob, wal_blob, page_key)
            shards.append(database.SqliteShard(shard_id, image, str(meta["accountId"]), self_username))
        contact_ids = {str(k): str(v) for k, v in (meta.get("contactIds") or {}).items()}
        contact_meta = meta.get("contactMeta") or {}
        store = database.ContactStore(str(meta["accountId"]), shards, contact_ids, contact_meta)
        return {
            "snapshotId": meta["snapshotId"],
            "accountId": meta["accountId"],
            "contactId": meta["contactId"],
            "cutoffTime": int(meta["cutoffTime"]),
            "total": int(meta["total"]),
            "username": meta["username"],
            "store": store,
        }

    def open_history(self, params: dict) -> dict:
        self._require_ready()
        if params.get("accountId") != self.account_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "account mismatch")
        contact_id = params.get("contactId")
        if not isinstance(contact_id, str) or not contact_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "contactId required")
        existing = params.get("snapshotId")
        if existing:
            meta = self.store.load_meta(str(existing))
            if meta.get("accountId") != self.account_id:
                raise errors.ProtocolError(errors.SNAPSHOT_ERROR, "snapshot account mismatch")
            if meta.get("contactId") != contact_id:
                raise errors.ProtocolError(errors.SNAPSHOT_ERROR, "snapshot contact mismatch")
            record = self._record_from_meta(meta)
            self._remember_snapshot(record["snapshotId"], record)
            high_watermark = record["store"].last_key(record["username"], record["cutoffTime"])
            return {
                "snapshotId": record["snapshotId"],
                "total": record["total"],
                "cursor": None,
                "highWatermark": database.encode_cursor(high_watermark),
            }

        # Freeze a consistent snapshot: refresh the live store first so `total` matches the bytes
        # captured below, then capture the ciphertext dataset.
        if self.mode == "live":
            self._refresh_live_store_if_stale()
        username = self.store_handle.username_for(contact_id)
        if username is None:
            raise errors.ProtocolError(errors.NOT_FOUND, "contact not found")
        cutoff_raw = params.get("cutoffTime")
        cutoff = int(cutoff_raw) if cutoff_raw is not None else (1 << 62)
        total = self.store_handle.total(username, cutoff)
        dataset_id = self._capture_dataset(cutoff)
        meta = {
            "accountId": self.account_id,
            "contactId": contact_id,
            "username": username,
            "cutoffTime": cutoff,
            "total": total,
            "mode": self.mode,
            "datasetId": dataset_id,
            "selfUsername": self._self_username(),
            "contactIds": {u: self.store_handle.contact_id_for(u) for u in self.store_handle._usernames},
            "contactMeta": self.store_handle._contact_meta,
        }
        payload = self.store.create_snapshot(meta)
        record = self._record_from_meta(payload)
        self._remember_snapshot(record["snapshotId"], record)
        high_watermark = record["store"].last_key(record["username"], record["cutoffTime"])
        return {
            "snapshotId": record["snapshotId"],
            "total": total,
            "cursor": None,
            "highWatermark": database.encode_cursor(high_watermark),
        }

    def _snapshot_record(self, snapshot_id: str) -> dict:
        record = self._snapshots.get(snapshot_id)
        if record is not None:
            return record
        meta = self.store.load_meta(snapshot_id)
        if meta.get("accountId") != self.account_id:
            raise errors.ProtocolError(errors.SNAPSHOT_ERROR, "snapshot account mismatch")
        record = self._record_from_meta(meta)
        self._remember_snapshot(snapshot_id, record)
        return record

    def read_history(self, params: dict) -> dict:
        self._require_ready()
        snapshot_id = params.get("snapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "snapshotId required")
        record = self._snapshot_record(snapshot_id)
        limit = _int_param(params.get("limit"), 200, 1, 500, "limit")
        context_limit = _int_param(params.get("contextLimit"), 0, 0, 32, "contextLimit")
        cursor = database.decode_cursor(params.get("cursor"))
        store = record["store"]
        rows, next_cursor, done = store.page(record["username"], record["cutoffTime"], cursor, limit)
        context = store.context_before(record["username"], record["cutoffTime"], cursor, context_limit)
        return {
            "rows": rows,
            "context": context,
            "nextCursor": database.encode_cursor(next_cursor),
            "done": done,
            "total": record["total"],
        }

    def read_incremental(self, params: dict) -> dict:
        """Bounded live-tail page: newest baseline (cursor null) or rows strictly after a cursor."""
        self._require_ready()
        account_id = params.get("accountId")
        if account_id is not None and account_id != self.account_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "account mismatch")
        contact_id = params.get("contactId")
        if not isinstance(contact_id, str) or not contact_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "contactId required")
        limit = _int_param(params.get("limit"), 120, 1, 500, "limit")
        context_limit = _int_param(params.get("contextLimit"), 0, 0, 32, "contextLimit")
        overlap_limit = _int_param(params.get("overlapLimit"), 0, 0, 120, "overlapLimit")
        if self.mode == "live":
            self._refresh_live_store_if_stale()
        username = self.store_handle.username_for(contact_id)
        if username is None:
            raise errors.ProtocolError(errors.NOT_FOUND, "contact not found")
        cursor = database.decode_cursor(params.get("cursor"))
        store = self.store_handle
        if cursor is None:
            rows = store.latest(username, limit)
            if rows:
                first_key = (int(rows[0]["sortSeq"]), str(rows[0]["shardId"]), int(rows[0]["localId"]))
                last_key = (int(rows[-1]["sortSeq"]), str(rows[-1]["shardId"]), int(rows[-1]["localId"]))
                context = store.context_before(username, 1 << 62, first_key, context_limit, inclusive=False)
                next_cursor = last_key
            else:
                context = []
                next_cursor = None
            return {
                "rows": rows,
                "context": context,
                "nextCursor": database.encode_cursor(next_cursor),
                "done": True,
            }
        # Non-null cursor: [last overlapLimit rows before cursor] + [up to limit rows strictly
        # after cursor], ascending. `nextCursor` only advances to the last strict-after row.
        after, _page_next, done = store.page(username, 1 << 62, cursor, limit)
        if after:
            last = after[-1]
            next_cursor = (int(last["sortSeq"]), str(last["shardId"]), int(last["localId"]))
        else:
            next_cursor = cursor
        overlap = store.context_before(username, 1 << 62, cursor, overlap_limit) if overlap_limit else []
        rows = overlap + after
        if rows:
            first_key = (int(rows[0]["sortSeq"]), str(rows[0]["shardId"]), int(rows[0]["localId"]))
            context = store.context_before(username, 1 << 62, first_key, context_limit, inclusive=False)
        else:
            context = []
        return {
            "rows": rows,
            "context": context,
            "nextCursor": database.encode_cursor(next_cursor),
            "done": done,
        }

    def latest(self, params: dict) -> dict:
        self._require_ready()
        account_id = params.get("accountId")
        if account_id is not None and account_id != self.account_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "account mismatch")
        contact_id = params.get("contactId")
        if not isinstance(contact_id, str) or not contact_id:
            raise errors.ProtocolError(errors.BAD_REQUEST, "contactId required")
        if self.mode == "live":
            self._refresh_live_store_if_stale()
        username = self.store_handle.username_for(contact_id)
        if username is None:
            raise errors.ProtocolError(errors.NOT_FOUND, "contact not found")
        limit = _int_param(params.get("limit"), 120, 1, 200, "limit")
        return {"rows": self.store_handle.latest(username, limit)}

    def window_method(self, params: dict | None = None) -> dict:
        params = params or {}
        pids = {process.pid for process in self._processes} if self._processes else {
            process.pid for process in discovery.find_weixin_processes()
        }
        # Capture is opt-in: geometry-only by default so no screenshot is taken implicitly.
        return window.current_geometry(pids, capture=bool(params.get("capture")))

    def window_uia_method(self, params: dict | None = None) -> dict:
        """Bounded, read-only UI Automation survey of the foreground Weixin main window."""
        params = params or {}
        pids = {process.pid for process in self._processes} if self._processes else {
            process.pid for process in discovery.find_weixin_processes()
        }
        geometry = window.enumerate_windows(pids)
        if not geometry:
            return {"ok": False, "reason": "no-window"}
        primary = next((w for w in geometry if w.foreground), geometry[0])
        max_nodes = min(int(params.get("maxNodes") or 1000), 1000)
        max_depth = min(int(params.get("maxDepth") or 12), 12)
        from . import window_uia
        return window_uia.probe_window(primary.hwnd, max_nodes=max_nodes, max_depth=max_depth)

    def close(self) -> dict:
        if self.store_handle is not None:
            self.store_handle.close()
            self.store_handle = None
        for record in self._snapshots.values():
            record["store"].close()
        self._snapshots.clear()
        for account in self.accounts.values():
            account.page_key = None
            account.keys_by_salt = {}
        self.account_id = None
        self._store_fingerprint = None
        self.state = "disconnected"
        self.message = ""
        return {"closed": True}

"""Cache-first, read-only preparation for the uniquely active WeChat account.

Only validated raw database keys live in this process memory. The inherited WeChatDB reader
still writes its ordinary decrypted SQLite snapshots under its account-specific workdir.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cache_source import (MESSAGE_DATABASE, CacheOnlyWeChatDB, IncompleteKeyCache,
                          database_groups, resolve_anchor_rels)
from wechatauto import db as upstream_db
from windows_file_owners import file_owners

NATIVE_READER = str(Path(__file__).resolve().parents[1] / "native-reader")
if NATIVE_READER not in sys.path:
    sys.path.insert(0, NATIVE_READER)
from wr import crypto, discovery

PAGE_SIZE = 4096
MAX_DB_FILES = 128
RETRY_SECONDS = 30
CONFIG_SCAN_CHUNK = 8 * 1024 * 1024
CONFIG_SCAN_PASS_BYTES = 2 * 1024 * 1024 * 1024
CONFIG_SCAN_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
CONFIG_SCAN_REGIONS = 65_536
CONFIG_SCAN_CANDIDATES = 256
CONFIG_SCAN_SECONDS = 90
CONFIG_SCAN_ANCHORS = 16_384
CONFIG_SCAN_PAIR_HITS = 16_384
CONFIG_SCAN_METRICS = ("anchors", "pairs", "scan_bytes", "scan_regions",
                       "read_gaps", "candidates")


@dataclass(frozen=True)
class ActiveSelection:
    account_dir: Path
    # A PID alone may be reused. Unknown creation times never authorize a memory scan.
    processes: tuple[tuple[int, float | None], ...]


def _account_database_files(account):
    root = Path(account.path).resolve()
    files = []
    anchors = []
    def unreadable(error):
        raise error
    for base, directories, names in os.walk(root, onerror=unreadable):
        directories[:] = [name for name in directories if not name.lower().startswith("migrate")]
        for name in names:
            if name.lower().endswith((".db", ".db-wal", ".db-shm")):
                path = (Path(base) / name).resolve()
                if not path.is_relative_to(root):
                    raise OSError("database file is outside the account directory")
                files.append(str(path))
                if name.casefold() in ("session.db", "contact.db", "session.db-wal", "contact.db-wal"):
                    anchors.append(str(path))
                if len(files) > MAX_DB_FILES * 4:
                    raise OSError("too many database resources for account discovery")
    # The role may live outside the usual folder; ownership remains the account proof.
    return anchors or files


def _account_from_file_owners(accounts, processes):
    """Ask Windows which discovered process owns each account's SQLite files."""
    known = {process.pid for process in processes}
    matches = {}
    for account in accounts:
        owned = set()
        for pid, created in file_owners(_account_database_files(account)):
            if pid not in known or discovery.psutil is None:
                continue
            try:
                process = discovery.psutil.Process(pid)
                # Recheck name and creation time: a reused PID is not a WeChat identity.
                if (process.name().lower() in ("weixin.exe", "wechat.exe") and
                        abs(process.create_time() - created) < .001):
                    owned.add((pid, created))
            except (discovery.psutil.NoSuchProcess, discovery.psutil.AccessDenied):
                continue
        if owned:
            location = Path(account.path).parent.resolve()
            matches.setdefault(location, set()).update(owned)
    if len(matches) != 1:
        return None
    location, owners = next(iter(matches.items()))
    return ActiveSelection(location, tuple(sorted(owners)))


def active_account_snapshot() -> ActiveSelection | None:
    """Use live file ownership only; ambiguous or logged-out accounts are not selected."""
    accounts = discovery.discover_account_dirs()
    processes = discovery.find_weixin_processes()
    if not accounts or not processes:
        return None
    if os.name == "nt":
        # Restart Manager only queries ownership. Unlike open_files(), this does not need
        # PROCESS_DUP_HANDLE, which current WeChat can deny even to the same Windows user.
        return _account_from_file_owners(accounts, processes)
    roots = [(account, os.path.normcase(os.path.realpath(account.path))) for account in accounts]
    matches: dict[str, set[tuple[int, float | None]]] = {}
    for process in processes:
        paths = discovery.process_open_file_paths(process.pid)
        if not paths:
            continue
        created = None
        try:
            if discovery.psutil is not None:
                created = float(discovery.psutil.Process(process.pid).create_time())
        except Exception:
            pass
        for path in paths:
            opened = os.path.normcase(os.path.realpath(path))
            for account, root in roots:
                if opened == root or opened.startswith(root + os.sep):
                    matches.setdefault(account.id, set()).add((process.pid, created))
    if len(matches) != 1:
        return None
    active_id, processes = next(iter(matches.items()))
    match = next((account for account in accounts if account.id == active_id), None)
    return (ActiveSelection(Path(match.path).parent.resolve(), tuple(sorted(processes)))
            if match else None)


def _pages(account_dir: Path) -> dict[str, tuple[Path, bytes, bytes]]:
    """Read page 1 for role candidates and current message shards."""
    storage = account_dir / "db_storage"
    storage_root = storage.resolve()
    pages = {}
    for base, _dirs, names in os.walk(storage):
        relative_base = os.path.relpath(base, storage)
        if os.path.normcase(relative_base).startswith("migrate"):
            continue
        for name in names:
            if not name.endswith(".db"):
                continue
            path = Path(base) / name
            rel = os.path.relpath(path, storage)
            normalized = rel.replace("\\", "/")
            if name.casefold() not in ("session.db", "contact.db") and not MESSAGE_DATABASE.fullmatch(normalized):
                continue
            if not path.resolve().is_relative_to(storage_root):
                raise RuntimeError("database file is outside the selected account")
            with open(path, "rb") as source:
                page = source.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE or page.startswith(b"SQLite format 3\x00"):
                raise RuntimeError("unsupported active database page layout")
            pages[rel] = (path, page[:16], page)
            if len(pages) > MAX_DB_FILES:
                raise RuntimeError("too many active database files")
    if not pages:
        raise RuntimeError("no active database files")
    return pages


def _token(selection: ActiveSelection, pages: dict[str, tuple[Path, bytes, bytes]],
           anchors_only=False):
    anchors, messages = database_groups(pages)
    return (os.path.normcase(os.path.realpath(selection.account_dir)), selection.processes,
            tuple(sorted((rel, pages[rel][1]) for rel in (anchors if anchors_only else anchors | messages))))


def _readiness(pages, keys):
    anchors, messages = database_groups(pages)
    valid_anchors = {rel for rel in anchors if rel in keys and
                     _valid_page_key(keys[rel], pages[rel][2])}
    sessions_ready = resolve_anchor_rels(pages, valid_anchors) is not None
    messages_ready = (sessions_ready and bool(messages) and
                      all(rel in keys and _valid_page_key(keys[rel], pages[rel][2])
                          for rel in messages))
    return sessions_ready, messages_ready


def _valid_page_key(raw, page):
    if not isinstance(raw, bytes) or len(raw) not in (32, 48):
        return False
    try:
        return upstream_db._verify_enc_key(raw, page)
    except Exception:
        return False


def _database_counts(pages, keys):
    counts = {category: [0, 0] for category in ("session", "contact", "message", "other")}
    for rel in pages:
        normalized = rel.replace("\\", "/")
        basename = normalized.rsplit("/", 1)[-1].casefold()
        category = ("session" if basename == "session.db" else
                    "contact" if basename == "contact.db" else
                    "message" if MESSAGE_DATABASE.fullmatch(normalized) else "other")
        counts[category][1] += 1
        counts[category][0] += rel in keys
    return " ".join(f"{name}={matched}/{required}"
                    for name, (matched, required) in counts.items())


class _ConfigScanLimit(RuntimeError):
    pass


class _AnchorAddresses(list):
    """Preserve upstream pair order while making its per-hit membership check constant time."""

    def __init__(self):
        super().__init__()
        self._lookup: set[int] = set()

    def append(self, address):
        self._lookup.add(address)
        super().append(address)

    def __contains__(self, address):
        return address in self._lookup


class _BoundedConfigCipherProbe:
    """Supply bounded address search to the installed reader's Config.Cipher decoder.

    This object deliberately has no account enumeration, cache, or master-key methods. The
    upstream decoder receives only the already-selected process and database files.
    """

    def __init__(self, pages: dict[str, tuple[Path, bytes, bytes]]):
        self._db_files = [(rel, str(path), len(page))
                          for rel, (path, _salt, page) in pages.items()]
        self._anchors = _AnchorAddresses()
        self._pair_hits: dict[int, list[int]] | None = None
        self._pairs = 0
        self._bytes_read = 0
        self._regions = 0
        self._read_gaps = 0
        self._candidates = 0
        self._limit_hits: set[str] = set()
        self._deadline = time.monotonic() + CONFIG_SCAN_SECONDS

    def metrics(self):
        return {"anchors": len(self._anchors), "pairs": self._pairs,
                "scan_bytes": self._bytes_read, "scan_regions": self._regions,
                "read_gaps": self._read_gaps,
                "candidates": self._candidates,
                "limit_hit": ",".join(sorted(self._limit_hits)) or "none"}

    def _check_deadline(self):
        if time.monotonic() >= self._deadline:
            self._limit_hits.add("time_limit")
            raise _ConfigScanLimit("time_limit")

    def _probable_key(self, candidate: bytes) -> bool:
        self._check_deadline()
        if self._candidates >= CONFIG_SCAN_CANDIDATES:
            self._limit_hits.add("candidate_limit")
            raise _ConfigScanLimit("candidate_limit")
        self._candidates += 1
        return upstream_db.WeChatDB._probable_key(candidate)

    def _scan_memory(self, handle, read, overlap_size: int, on_data):
        """Read bounded regions once per pass; retry short reads within their original span."""
        address = 0
        pass_bytes = 0
        while True:
            self._check_deadline()
            if self._regions >= CONFIG_SCAN_REGIONS:
                self._limit_hits.add("region_limit")
                return
            info = upstream_db._MBI()
            if not upstream_db._k32.VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)
            ):
                return
            self._regions += 1
            base = int(info.BaseAddress or 0)
            size = int(info.RegionSize)
            next_address = base + size
            if size <= 0 or next_address <= address:
                return
            readable = (info.State == 0x1000 and (info.Protect & 0xFF) & 0xE6
                        and not (info.Protect & 0x100))
            if readable:
                offset = 0
                overlap = b""
                while offset < size:
                    self._check_deadline()
                    budget = min(CONFIG_SCAN_PASS_BYTES - pass_bytes,
                                 CONFIG_SCAN_TOTAL_BYTES - self._bytes_read)
                    if budget <= 0:
                        self._limit_hits.add("byte_limit")
                        return
                    window_end = offset + min(CONFIG_SCAN_CHUNK, size - offset, budget)
                    read_size = window_end - offset
                    while offset < window_end:
                        self._check_deadline()
                        budget = min(CONFIG_SCAN_PASS_BYTES - pass_bytes,
                                     CONFIG_SCAN_TOTAL_BYTES - self._bytes_read)
                        if budget <= 0:
                            self._limit_hits.add("byte_limit")
                            return
                        take = min(read_size, window_end - offset, budget)
                        if take <= PAGE_SIZE:
                            # A failed page read must not discard readable bytes before
                            # the next page boundary after a short read.
                            take = min(take, PAGE_SIZE - (base + offset) % PAGE_SIZE)
                        self._bytes_read += take
                        pass_bytes += take
                        chunk = (read(base + offset, take) or b"")[:take]
                        if chunk:
                            fresh_at = len(overlap)
                            data = overlap + chunk
                            data_base = base + offset - fresh_at
                            if on_data(data, data_base, fresh_at):
                                return
                            overlap = data[-overlap_size:] if overlap_size else b""
                            offset += len(chunk)
                        if len(chunk) < take:
                            if take > PAGE_SIZE:
                                read_size = max(PAGE_SIZE, take // 2)
                            elif not chunk:
                                # Only a page segment that still fails at the safe floor
                                # is an unrecovered gap. Never join matches across it.
                                self._read_gaps += 1
                                self._limit_hits.add("read_gap")
                                overlap = b""
                                offset += min(size - offset,
                                              PAGE_SIZE - (base + offset) % PAGE_SIZE)
                                read_size = window_end - offset
                        else:
                            read_size = min(window_end - offset, max(read_size, take * 2))
            address = next_address

    def _scan_anchors(self, handle, read):
        needle = upstream_db.CONFIG_CIPHER_NAME

        def collect(data, data_base, fresh_at):
            at = 0
            while True:
                at = data.find(needle, at)
                if at < 0:
                    return False
                if at + len(needle) <= fresh_at:
                    at += 1
                    continue
                self._anchors.append(data_base + at)
                if len(self._anchors) >= CONFIG_SCAN_ANCHORS:
                    self._limit_hits.add("anchor_limit")
                    return True
                at += 1

        self._scan_memory(handle, read, len(needle) - 1, collect)

    def _scan_pairs(self, handle, read):
        marker = struct.pack("<Q", len(upstream_db.CONFIG_CIPHER_NAME))
        anchors = set(self._anchors)
        self._pair_hits = {}
        marker_count = 0

        def collect(data, data_base, fresh_at):
            nonlocal marker_count
            at = 0
            while True:
                at = data.find(marker, at)
                if at < 0:
                    return False
                marker_count += 1
                if marker_count % 1024 == 0:
                    self._check_deadline()
                if at >= 8 and at + len(marker) > fresh_at:
                    anchor = struct.unpack_from("<Q", data, at - 8)[0]
                    if anchor in anchors:
                        self._pair_hits.setdefault(anchor, []).append(data_base + at - 8)
                        self._pairs += 1
                        if self._pairs >= CONFIG_SCAN_PAIR_HITS:
                            self._limit_hits.add("pair_limit")
                            return True
                at += 1

        # A pair is 16 bytes; 15 bytes of overlap catches pairs crossing chunks.
        self._scan_memory(handle, read, 15, collect)

    def _pair_addresses(self, anchor):
        for address in self._pair_hits.get(anchor, ()):
            self._check_deadline()
            yield address

    def _find_bytes(self, handle, read, needle: bytes):
        if needle == upstream_db.CONFIG_CIPHER_NAME:
            self._scan_anchors(handle, read)
            return self._anchors
        if len(needle) != 16 or needle[8:] != struct.pack("<Q", len(upstream_db.CONFIG_CIPHER_NAME)):
            return ()
        if self._pair_hits is None:
            self._scan_pairs(handle, read)
        anchor = struct.unpack_from("<Q", needle)[0]
        return self._pair_addresses(anchor)


def _scan_config_cipher_keys(pid: int, pages: dict[str, tuple[Path, bytes, bytes]],
                             metrics: dict | None = None):
    probe = _BoundedConfigCipherProbe(pages)
    try:
        return upstream_db.WeChatDB._extract_keys_pid(probe, pid, set())
    finally:
        if metrics is not None:
            metrics.update(probe.metrics())


class VolatileKeyWeChatDB(CacheOnlyWeChatDB):
    """Use validated in-memory per-database keys when existing key caches are absent."""

    def __init__(self, *, volatile_keys: dict[str, bytes], **kwargs):
        self._volatile_keys = dict(volatile_keys)
        super().__init__(**kwargs)

    def _load_or_extract_keys(self, master_key=None):
        cached = {}
        try:
            return super()._load_or_extract_keys(master_key=master_key)
        except IncompleteKeyCache as exc:
            cached = exc.validated_keys
        except RuntimeError:
            if master_key is not None:
                raise
        self.master_key = None
        self.cfg_dword = None
        self._keys = dict(cached)
        files = {rel for rel, _path, _size in self._db_files}
        anchors, messages = database_groups(files)
        required = anchors | messages
        for rel in tuple(self._keys):
            if rel not in required or not self._key_works(rel):
                del self._keys[rel]
        for rel in required - self._keys.keys():
            raw = self._volatile_keys.get(rel)
            if isinstance(raw, bytes) and len(raw) in (32, 48):
                self._keys[rel] = raw
                if not self._key_works(rel):
                    del self._keys[rel]
        selected = resolve_anchor_rels(files, self._keys)
        if selected is None or not messages or (set(selected.values()) | messages) - self._keys.keys():
            self._keys = {}
            raise RuntimeError("local WeChat key cache unavailable for selected account")
        self.anchor_rels = selected
        self.messages_ready = True

    def _save_keys(self, *args, **kwargs):
        # The upstream new-shard path must never persist volatile key material.
        return None


class SessionOnlyWeChatDB(VolatileKeyWeChatDB):
    """A deliberately narrow reader that cannot open a message or unrelated database."""

    def _collect_db_files(self):
        files = super()._collect_db_files()
        anchors, _messages = database_groups(rel for rel, _path, _size in files)
        return [item for item in files if item[0] in anchors]

    def _load_or_extract_keys(self, master_key=None):
        try:
            CacheOnlyWeChatDB._load_or_extract_keys(self, master_key=master_key)
        except IncompleteKeyCache as exc:
            cached = exc.validated_keys
        else:
            cached = dict(self._keys)
        self.master_key = None
        self.cfg_dword = None
        self._keys = dict(cached)
        candidates, _messages = database_groups(rel for rel, _path, _size in self._db_files)
        for rel in candidates:
            if rel in self._keys:
                continue
            raw = self._volatile_keys.get(rel)
            if isinstance(raw, bytes) and len(raw) in (32, 48):
                self._keys[rel] = raw
                if not self._key_works(rel):
                    del self._keys[rel]
        selected = resolve_anchor_rels(candidates, self._keys)
        if selected is None:
            self._keys = {}
            raise RuntimeError("local WeChat session keys unavailable for selected account")
        self.anchor_rels = selected
        self._db_files = [item for item in self._db_files if item[0] in selected.values()]
        self._keys = {rel: self._keys[rel] for rel in selected.values()}
        self.messages_ready = False

    def _open(self, rel):
        if rel not in self.anchor_rels.values():
            raise RuntimeError("messages unavailable for selected account")
        return super()._open(rel)

    def _message_dbs(self):
        raise RuntimeError("messages unavailable for selected account")


@dataclass
class _ScanSlot:
    state: str
    keys: dict[str, bytes] | None = None
    retry_at: float = 0.0
    scanning: bool = False


class LiveWeChatFactory:
    """One background scan per account/process/page-salt identity, with bounded retry."""

    def __init__(self, cache_factory=CacheOnlyWeChatDB, volatile_factory=VolatileKeyWeChatDB,
                 scanner=None, retry_seconds=RETRY_SECONDS,
                 session_factory=SessionOnlyWeChatDB):
        self.cache_factory = cache_factory
        self.volatile_factory = volatile_factory
        self.session_factory = session_factory
        self.scanner = scanner
        self.retry_seconds = retry_seconds
        self.lock = threading.Lock()
        self.slots: dict[tuple, _ScanSlot] = {}

    def forget_account(self, account):
        """Discard scoped in-memory keys; a running scan will discard its late result."""
        with self.lock:
            for token in list(self.slots):
                if os.path.basename(token[0]) == account:
                    del self.slots[token]

    def __call__(self, *, db_dir: str, account: str, selection: ActiveSelection | None):
        location = (Path(db_dir) / account).resolve()
        if selection is None or selection.account_dir != location:
            raise RuntimeError("active account changed")
        # An explicit account's existing cache can support layouts the volatile scanner cannot.
        cached_keys = {}
        try:
            return self.cache_factory(db_dir=db_dir, account=account)
        except IncompleteKeyCache as exc:
            cached_keys = exc.validated_keys
        except Exception:
            pass
        pages = _pages(location)
        token = _token(selection, pages)
        start_scan = False
        with self.lock:
            # Only the current account/process/page identity may retain volatile keys.
            for stale in tuple(self.slots):
                if stale != token:
                    del self.slots[stale]
            slot = self.slots.get(token)
            if slot and slot.state in ("ready", "sessions"):
                keys = dict(slot.keys or {})
                reader_state = slot.state
                if (slot.state == "sessions" and not slot.scanning and
                        time.monotonic() >= slot.retry_at):
                    slot.scanning = True
                    start_scan = True
            elif slot and (slot.state == "running" or time.monotonic() < slot.retry_at):
                raise RuntimeError("active account preparation pending")
            else:
                keys = None
                reader_state = None
                slot = _ScanSlot("running", scanning=True)
                self.slots[token] = slot
                start_scan = True
        if start_scan:
            threading.Thread(target=self._scan,
                             args=(selection, pages, token, {**cached_keys, **(keys or {})}, slot),
                             daemon=True).start()
        if keys is None:
            raise RuntimeError("active account preparation pending")
        factory = self.volatile_factory if reader_state == "ready" else self.session_factory
        try:
            return factory(db_dir=db_dir, account=account, volatile_keys=keys)
        except Exception:
            with self.lock:
                if self.slots.get(token) is slot and slot.state == reader_state:
                    slot.state, slot.keys = "failed", None
                    slot.retry_at = time.monotonic() + self.retry_seconds
            raise RuntimeError("active account preparation pending") from None

    def _scan(self, selection: ActiveSelection, pages, token, cached_keys=None, slot=None):
        keys: dict[str, bytes] = {}
        reason = "keys_incomplete"
        stage = "cache_validation"
        matched = 0
        matched_rels = set()
        cache_count = 0
        scan_new_count = 0
        scan_counts = {name: 0 for name in CONFIG_SCAN_METRICS}
        limit_hits: set[str] = set()
        try:
            for rel, raw in (cached_keys or {}).items():
                if rel in pages and _valid_page_key(raw, pages[rel][2]):
                    keys[rel] = raw
            cache_count = len(keys)
            first_pages = {}
            for _rel, (_path, salt, page) in pages.items():
                first_pages.setdefault(salt, page)
            for pid, created in selection.processes:
                stage = "process_identity"
                if created is None:
                    reason = "process_identity_unavailable"
                    break
                if active_account_snapshot() != selection:
                    reason = "account_changed"
                    break
                stage = "process_scan"
                stage = "candidate_validation"
                if self.scanner is None:
                    stage = "config_cipher_scan"
                    scan_metrics = {}
                    try:
                        found = _scan_config_cipher_keys(pid, pages, scan_metrics)
                    finally:
                        for name in CONFIG_SCAN_METRICS:
                            scan_counts[name] += scan_metrics.get(name, 0)
                        if scan_metrics.get("limit_hit", "none") != "none":
                            limit_hits.update(scan_metrics["limit_hit"].split(","))
                    stage = "candidate_validation"
                    for rel, raw in found.items():
                        if (rel in pages and rel not in keys and
                                _valid_page_key(raw, pages[rel][2])):
                            keys[rel] = raw
                            scan_new_count += 1
                else:
                    candidates = self.scanner(pid, set(first_pages), first_pages)
                    for candidate in candidates:
                        for rel, (_path, salt, page) in pages.items():
                            if rel in keys or candidate.salt != salt:
                                continue
                            if crypto.validate_key(candidate.key, salt, page) is not None:
                                keys[rel] = candidate.key
                                scan_new_count += 1
                if len(keys) == len(pages):
                    break
        except _ConfigScanLimit as exc:
            limit_hits.add(str(exc))
            reason = "scan_limit"
        except discovery.errors.ProtocolError as exc:
            reason = ("process_read_denied" if exc.code == discovery.errors.PERMISSION_DENIED
                      else "process_scan_error")
        except Exception:
            reason = stage + "_error"
        try:
            stage = "final_identity"
            if active_account_snapshot() != selection:
                reason = "account_changed"
                keys = {}
            else:
                stage = "final_pages"
                current_pages = _pages(selection.account_dir)
                if _token(selection, current_pages) != token:
                    reason = "database_changed"
                    keys = {}
                else:
                    keys = {rel: raw for rel, raw in keys.items()
                            if rel in current_pages and _valid_page_key(raw, current_pages[rel][2])}
                    sessions_ready, messages_ready = _readiness(current_pages, keys)
                    if messages_ready:
                        reason = "ready"
                    elif limit_hits:
                        reason = "scan_limit"
                    elif reason == "ready":
                        reason = "keys_incomplete"
        except Exception:
            reason = stage + "_error"
            keys = {}
        matched = len(keys)
        matched_rels = set(keys)
        sessions_ready, messages_ready = _readiness(pages, keys)
        with self.lock:
            if self.slots.get(token) is not slot or not slot.scanning:
                reason = "scan_discarded"
            else:
                slot.scanning = False
                if messages_ready:
                    slot.state, slot.keys = "ready", keys
                elif sessions_ready:
                    slot.state, slot.keys = "sessions", keys
                    slot.retry_at = time.monotonic() + self.retry_seconds
                else:
                    slot.state, slot.keys = "failed", None
                    slot.retry_at = time.monotonic() + self.retry_seconds
        print(f"live_wechat_preparation result={reason} required={len(pages)} "
              f"matched={matched} cache={cache_count} scan_new={scan_new_count} union={matched} "
              f"{_database_counts(pages, matched_rels)} "
              f"anchors={scan_counts['anchors']} pairs={scan_counts['pairs']} "
              f"candidates={scan_counts['candidates']} scan_bytes={scan_counts['scan_bytes']} "
              f"scan_regions={scan_counts['scan_regions']} read_gaps={scan_counts['read_gaps']} "
              f"limit_hit={','.join(sorted(limit_hits)) or 'none'}",
              file=sys.stderr, flush=True)

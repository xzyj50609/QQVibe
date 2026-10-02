"""Reuse account-specific SQLite snapshots; refresh changed sources in the background.

The installed reader still performs all decryption and WAL validation. Refreshing into a
separate work directory preserves the last usable database while a new snapshot is built.
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from wechatauto import db as upstream

_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wechat-snapshot")
_states_lock = threading.Lock()
_states = {}
_log = logging.getLogger(__name__)
CONTACT_REFRESH_WAIT = 1.5


class ContactSnapshotStaleError(RuntimeError):
    """The current contact source is newer than every safe readable snapshot."""


class _State:
    def __init__(self):
        self.lock = threading.RLock()
        self.future = None
        self.retry_at = 0.0
        self.last_error = None


def _state(reader, rel):
    key = (os.path.normcase(os.path.realpath(reader.account_dir)),
           os.path.normcase(os.path.realpath(reader.workdir)), rel)
    with _states_lock:
        return _states.setdefault(key, _State())


def _destination(reader, rel):
    root = Path(reader.workdir).resolve()
    path = (root / rel.replace(os.sep, "__")).resolve()
    if path.parent != root:
        raise RuntimeError("invalid snapshot path")
    return path


def _source_signature(reader, rel):
    source = os.stat(reader._db_path(rel))
    wal_path = reader._wal_path(rel)
    try:
        wal = os.stat(wal_path) if wal_path else None
    except FileNotFoundError:
        # A checkpoint may remove the WAL between discovery and stat.
        wal = None
    return (source.st_mtime, source.st_size,
            wal.st_mtime if wal else 0.0, wal.st_size if wal else 0)


def _stamp_details(destination):
    try:
        parts = Path(str(destination) + ".stamp").read_text(encoding="ascii").split(",")
        if len(parts) != 6 or int(parts[0]) != upstream.STAMP_VERSION:
            return None
        return ((float(parts[1]), int(parts[2]), float(parts[3]), int(parts[4])), int(parts[5]))
    except (OSError, ValueError):
        return None


def _saved_signature(destination):
    details = _stamp_details(destination)
    return details[0] if details else None


def _merged_contact_signature(destination):
    details = _stamp_details(destination)
    return details[0] if details and details[1] >= 0 else None


def _connect(destination):
    connection = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA schema_version").fetchone()
        connection.row_factory = sqlite3.Row
        connection.text_factory = upstream._sqlite_text_factory
        return connection
    except Exception:
        connection.close()
        raise


def _refresh(reader, rel, destination, state):
    try:
        staged = copy.copy(reader)
        staged._keys = dict(reader._keys)
        staged.workdir = str(Path(reader.workdir) / ".snapshot-refresh")
        Path(staged.workdir).mkdir(parents=True, exist_ok=True)
        built = _destination(staged, rel)
        if Path(rel).name.lower() == "contact.db":
            details = _stamp_details(built)
            if details is not None and details[1] < 0:
                Path(str(built) + ".stamp").unlink(missing_ok=True)
        connection = upstream.WeChatDB._open(staged, rel)
        connection.close()
        if _saved_signature(built) is None:
            raise RuntimeError("refreshed snapshot is unverified")
        if Path(rel).name.lower() == "contact.db" and _merged_contact_signature(built) is None:
            Path(str(built) + ".stamp").unlink(missing_ok=True)
            raise RuntimeError("contact snapshot WAL is not merged")
        # Existing readers may briefly hold the Windows file open. In that case keep the
        # current snapshot and retry on a later read; never interrupt its reader.
        pending = Path(str(destination) + ".refresh-ready")
        pending_stamp = Path(str(destination) + ".stamp.refresh-ready")
        shutil.copyfile(built, pending)
        shutil.copyfile(str(built) + ".stamp", pending_stamp)
        with state.lock:
            os.replace(pending, destination)
            os.replace(pending_stamp, str(destination) + ".stamp")
            state.retry_at = 0.0
            state.last_error = None
    except Exception as exc:
        # Do not log paths, keys or message contents. The valid old snapshot remains usable.
        with state.lock:
            state.retry_at = time.monotonic() + (10 if Path(rel).name.lower() == "contact.db" else 2)
            kind = type(exc).__name__
            if state.last_error != kind:
                _log.warning("snapshot refresh deferred (%s); retaining previous data", kind)
            state.last_error = kind


def try_forget_account(workdir):
    """Release only idle refresh state before deleting an inactive account's caches.

    The caller must hold the source lock and recheck the selected live account.
    This function never removes files or cancels an in-progress writer.
    """
    selected = os.path.normcase(os.path.realpath(workdir))
    with _states_lock:
        matches = [(key, state) for key, state in _states.items() if key[1] == selected]
        if any(state.future is not None and not state.future.done() for _, state in matches):
            return False
        for key, _ in matches:
            del _states[key]
    return True


def wait_forget_account(workdir, timeout=180):
    """Wait for this account's already-running snapshot writer before deleting its files."""
    selected = os.path.normcase(os.path.realpath(workdir))
    deadline = time.monotonic() + timeout
    with _states_lock:
        futures = [state.future for key, state in _states.items()
                   if key[1] == selected and state.future is not None and not state.future.done()]
    for future in futures:
        try:
            future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeoutError:
            return False
        except Exception:
            # A failed refresh is already finished; its partial staged files are still scoped.
            pass
    return try_forget_account(workdir)


class SnapshotCacheMixin:
    def _open(self, rel):
        state = _state(self, rel)
        destination = _destination(self, rel)
        contact = Path(rel).name.lower() == "contact.db"
        future = None
        with state.lock:
            signature = _source_signature(self, rel)
            saved = _saved_signature(destination)
            if destination.is_file() and saved is not None:
                if contact and _merged_contact_signature(destination) != signature:
                    if (time.monotonic() >= state.retry_at and
                            (state.future is None or state.future.done())):
                        state.future = _workers.submit(_refresh, self, rel, destination, state)
                    future = state.future
                else:
                    try:
                        connection = _connect(destination)
                    except sqlite3.DatabaseError:
                        # Force the existing reader to rebuild a corrupt cached SQLite file.
                        Path(str(destination) + ".stamp").unlink(missing_ok=True)
                        connection = upstream.WeChatDB._open(self, rel)
                        if contact and _merged_contact_signature(destination) != _source_signature(self, rel):
                            connection.close()
                            raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
                        return connection
                    if contact and _merged_contact_signature(destination) != _source_signature(self, rel):
                        connection.close()
                        raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
                    if (signature != saved and time.monotonic() >= state.retry_at
                            and (state.future is None or state.future.done())):
                        state.future = _workers.submit(_refresh, self, rel, destination, state)
                    return connection
            else:
                # Only a database without a usable snapshot blocks for its first preparation.
                connection = upstream.WeChatDB._open(self, rel)
                if contact and _merged_contact_signature(destination) != _source_signature(self, rel):
                    connection.close()
                    raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
                return connection
        if future is not None:
            try:
                future.result(timeout=CONTACT_REFRESH_WAIT)
            except FutureTimeoutError:
                pass
        with state.lock:
            if _merged_contact_signature(destination) == _source_signature(self, rel):
                try:
                    return _connect(destination)
                except sqlite3.DatabaseError:
                    pass
        raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")

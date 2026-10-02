"""QQ account registry and retryable deletion, strictly inside QQVibeData."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from account_store import (ACCOUNT_ID, AccountConflict, AccountNotFound, AccountStore,
                           _check_root, _regular, _reparse, _same_checked_directory, account_id)
from qq_identity import account_key, canonical_uin, is_account_key


class QQAccountStore(AccountStore):
    def __init__(self, data_root):
        # Deliberately do not construct the WeChat store/default snapshot/key paths.
        self.root = Path(os.path.abspath(data_root))
        self.data_dir = self.root / "real-client-data"
        self.snapshot_root = self.root / "accounts"
        self.deletion_root = self.root / ".account-deletion"
        self.registry = self.data_dir / "accounts.json"
        self.stable_keys_dir = None
        self.lock = threading.RLock()

    def _workdir(self, account):
        if not is_account_key(account):
            raise AccountConflict("QQ 账号标识无效")
        return self.snapshot_root / account[2:]

    def _stable_key_file(self, account):
        return None

    def _cache_files(self, account, strict=False):
        return self._owned_tree(account)[0]

    def _read_registry(self):
        _check_root(self.data_dir)
        if not _regular(self.registry):
            return {}
        try:
            document = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AccountConflict("QQ 账号索引不可读取") from exc
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("accounts"), list):
            raise AccountConflict("QQ 账号索引格式无效")
        result = {}
        for item in document["accounts"]:
            if not isinstance(item, dict) or not is_account_key(item.get("account")):
                continue
            account = item["account"]
            identifier = account_id(account)
            try:
                owner = canonical_uin(item.get("ownerUin"))
                workdir = Path(item["workdir"])
                if (account_key(owner) != account or item.get("accountId") != identifier or
                        not workdir.is_absolute() or ".." in workdir.parts or
                        not _same_checked_directory(workdir, self._workdir(account))):
                    continue
                deletion_id = item.get("deletionId")
                if deletion_id is not None and not re.fullmatch(identifier + r"-[0-9a-f]{32}", deletion_id):
                    continue
            except (ValueError, TypeError, KeyError, AccountConflict):
                continue
            result[identifier] = {"accountId": identifier, "account": account, "ownerUin": owner,
                                  "workdir": str(self._workdir(account)),
                                  "nickname": item.get("nickname") if isinstance(item.get("nickname"), str) else "",
                                  **({"deletionId": deletion_id, "deletionState": item.get("deletionState", "staging")}
                                     if deletion_id is not None else {})}
        return result

    def _discover(self):
        # Only our explicit registry. Never enumerate WeChat snapshots, keys or results.
        return self._read_registry()

    def register(self, account, workdir, owner_uin="", nickname=""):
        owner = canonical_uin(owner_uin)
        if account_key(owner) != account or not _same_checked_directory(Path(workdir), self._workdir(account)):
            raise AccountConflict("QQ 账号与本地库不匹配")
        with self.lock:
            records = self._read_registry()
            identifier = account_id(account)
            if records.get(identifier, {}).get("deletionId"):
                raise AccountConflict("此账号清理尚未完成，请先重试清理")
            item = {"accountId": identifier, "account": account, "ownerUin": owner,
                    "workdir": str(self._workdir(account)), "nickname": nickname or ""}
            if records.get(identifier) != item:
                records[identifier] = item
                self._write_registry(records)
            return identifier

    def is_deleting(self, account):
        with self.lock:
            return bool(self._read_registry().get(account_id(account), {}).get("deletionId"))

    def _checked_tree(self, root):
        _check_root(root)
        if not root.exists():
            return [], []
        if not root.is_dir() or not root.resolve().is_relative_to(self.root.resolve()):
            raise AccountConflict("QQ 清理目录不安全")
        files, directories, pending = [], [root], [root]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                if _reparse(path) or not path.resolve().is_relative_to(root.resolve()):
                    raise AccountConflict("QQ 清理目录不安全")
                if path.is_dir():
                    directories.append(path)
                    pending.append(path)
                elif _regular(path):
                    files.append(path)
                else:
                    raise AccountConflict("QQ 清理目录不安全")
        return files, sorted(directories, key=lambda path: len(path.parts), reverse=True)

    def _result_files(self, identifier):
        root = self.data_dir / (identifier + ".sqlite3")
        return [path for suffix in ("", "-journal", "-wal", "-shm")
                if _regular(path := Path(str(root) + suffix))]

    def list(self, current_account=None):
        with self.lock:
            current_id = account_id(current_account) if current_account else None
            items = []
            for identifier, item in self._read_registry().items():
                files = self._checked_tree(self._workdir(item["account"]))[0] + self._result_files(identifier)
                if item.get("deletionId"):
                    files += self._checked_tree(self.deletion_root / item["deletionId"])[0]
                items.append({"accountId": identifier, "displayId": item["ownerUin"], "platform": "qq",
                              "nickname": item["nickname"], "current": identifier == current_id,
                              "bytes": sum(path.stat().st_size for path in files),
                              "deletionPending": bool(item.get("deletionId"))})
            items.sort(key=lambda item: (not item["current"], item["displayId"], item["accountId"]))
            return {"accounts": items, "currentAccountId": current_id, "platform": "qq"}

    def _purge(self, directory, guard, account):
        files, directories = self._checked_tree(directory)
        for path in files:
            guard(account)
            _check_root(path.parent)
            if _regular(path):
                path.unlink()
        for path in directories:
            guard(account)
            _check_root(path)
            path.rmdir()

    def delete(self, identifier, *, guard, forget):
        if not isinstance(identifier, str) or not ACCOUNT_ID.fullmatch(identifier):
            raise ValueError("invalid accountId")
        with self.lock:
            records = self._read_registry()
            item = records.get(identifier)
            if item is None:
                raise AccountNotFound("账号不存在")
            account, workdir = item["account"], self._workdir(item["account"])
            guard(account)
            if not forget(workdir):
                raise AccountConflict("QQ 本地库仍在使用")
            # Validate every descendant before moving anything; no shell recursion.
            self._checked_tree(workdir)
            sources = ([workdir] if workdir.exists() else []) + self._result_files(identifier)
            was_pending = bool(item.get("deletionId"))
            original = dict(item)
            if not was_pending:
                item = {**item, "deletionId": identifier + "-" + uuid.uuid4().hex, "deletionState": "staging"}
                records[identifier] = item
            staging = self.deletion_root / item["deletionId"]
            _check_root(staging)
            staging.mkdir(parents=True, exist_ok=True)
            # Durable intent precedes moves, so a crash can resume the same deletion.
            self._write_registry(records)
            moved = []
            try:
                for source in sources:
                    target = staging / ("messages" if source == workdir else source.name)
                    if os.path.lexists(target):
                        raise AccountConflict("QQ 清理目标冲突，请保留现场")
                    guard(account)
                    _check_root(source)
                    _check_root(staging)
                    os.replace(source, target)
                    moved.append((source, target))
                item["deletionState"] = "purging"
                self._write_registry(records)
            except BaseException:
                for source, target in reversed(moved):
                    _check_root(source.parent)
                    _check_root(target)
                    os.replace(target, source)
                if not was_pending:
                    records[identifier] = original
                    self._write_registry(records)
                    if staging.is_dir() and not any(staging.iterdir()):
                        staging.rmdir()
                raise
            try:
                self._purge(staging, guard, account)
            except OSError as exc:
                # Do not hide leftover private data or reattach an empty new library.
                raise AccountConflict("账号清理尚未完成；本地数据已隔离，请重试清理") from exc
            records.pop(identifier)
            self._write_registry(records)
            return {"deleted": identifier}

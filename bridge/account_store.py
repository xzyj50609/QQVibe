"""Account index and narrowly scoped deletion of this app's derived data."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import uuid
from contextlib import closing
from pathlib import Path


ACCOUNT_ID = re.compile(r"[0-9a-f]{64}\Z")
SNAPSHOT_FILE = re.compile(
    r"[^/\\:]+__[^/\\:]+\.db(?:\.stamp(?:\.refresh-ready)?|\.tmp|\.wal|\.refresh-ready|-wal|-shm)?\Z"
)


class AccountConflict(RuntimeError):
    pass


class AccountNotFound(LookupError):
    pass


def account_id(account):
    return hashlib.sha256(account.encode("utf-8")).hexdigest()


def _safe_account(account):
    return (isinstance(account, str) and bool(account) and account not in (".", "..") and
            not any(char in account for char in "/\\:\0") and
            not any(ord(char) < 32 for char in account) and
            not account.endswith((".", " ")))


def _reparse(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ) or bool(getattr(os.path, "isjunction", lambda _path: False)(path)))


def _check_root(path):
    for part in (path, *path.parents):
        if _reparse(part):
            raise AccountConflict("账号缓存路径不安全")


def _regular(path):
    if not os.path.lexists(path):
        return False
    if _reparse(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise AccountConflict("账号缓存路径不安全")
    return True


def _same_checked_directory(left, right):
    """Compare directory identities only after rejecting reparse points in both paths."""
    _check_root(left)
    _check_root(right)
    return left.resolve() == right.resolve()


class AccountStore:
    def __init__(self, data_dir, snapshot_root=None, stable_keys_dir=None):
        self.data_dir = Path(os.path.abspath(data_dir))
        self.snapshot_root = Path(os.path.abspath(
            snapshot_root or Path(tempfile.gettempdir()) / "wechatauto_db"
        ))
        self.registry = self.data_dir / "accounts.json"
        if stable_keys_dir is None:
            configured = os.environ.get("WECHATAUTO_KEYS_DIR")
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
            stable_keys_dir = configured or (Path(base) / "wechatauto_keys" if base else None)
        self.stable_keys_dir = Path(os.path.abspath(stable_keys_dir)) if stable_keys_dir else None
        self.lock = threading.RLock()

    def _owned_tree(self, account):
        """List only the verified account-specific app cache directory, never WeChat's source."""
        workdir = self._workdir(account)
        _check_root(self.snapshot_root)
        if not os.path.lexists(workdir):
            return [], []
        if _reparse(workdir) or not workdir.is_dir():
            raise AccountConflict("账号缓存路径不安全")
        resolved_workdir = workdir.resolve()
        files, directories, pending = [], [workdir], [workdir]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                if _reparse(path) or not path.resolve().is_relative_to(resolved_workdir):
                    raise AccountConflict("账号缓存路径不安全")
                if path.is_dir():
                    directories.append(path)
                    pending.append(path)
                elif path.is_file():
                    files.append(path)
                else:
                    raise AccountConflict("账号缓存路径不安全")
        return files, sorted(directories, key=lambda path: len(path.parts), reverse=True)

    def _stable_key_file(self, account):
        if self.stable_keys_dir is None:
            return None
        _check_root(self.stable_keys_dir)
        path = self.stable_keys_dir / (account + ".json")
        if path.parent != self.stable_keys_dir or (_reparse(path) if os.path.lexists(path) else False):
            raise AccountConflict("账号密钥缓存路径不安全")
        return path

    def _workdir(self, account):
        if not _safe_account(account):
            raise AccountConflict("账号缓存路径不安全")
        path = self.snapshot_root / account
        if path.parent != self.snapshot_root:
            raise AccountConflict("账号缓存路径不安全")
        return path

    def _cache_files(self, account, strict=False):
        workdir = self._workdir(account)
        _check_root(self.snapshot_root)
        if not os.path.lexists(workdir):
            return []
        if _reparse(workdir) or not workdir.is_dir():
            if strict:
                raise AccountConflict("账号缓存路径不安全")
            return []
        files = []
        for directory in (workdir, workdir / ".snapshot-refresh"):
            if not os.path.lexists(directory):
                continue
            if _reparse(directory) or not directory.is_dir():
                if strict:
                    raise AccountConflict("账号缓存路径不安全")
                continue
            for path in directory.iterdir():
                if _reparse(path):
                    if strict:
                        raise AccountConflict("账号缓存路径不安全")
                    continue
                if SNAPSHOT_FILE.fullmatch(path.name):
                    if not path.is_file():
                        if strict:
                            raise AccountConflict("账号缓存路径不安全")
                        continue
                    files.append(path)
        return files

    def _read_registry(self):
        _check_root(self.data_dir)
        if not _regular(self.registry):
            return {}
        try:
            document = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccountConflict("账号索引不可读取") from exc
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("accounts"), list):
            raise AccountConflict("账号索引格式无效")
        found = {}
        for item in document["accounts"]:
            if not isinstance(item, dict):
                continue
            account = item.get("account")
            if not _safe_account(account) or item.get("accountId") != account_id(account):
                continue
            expected = self._workdir(account)
            stored = item.get("workdir")
            if not isinstance(stored, str):
                continue
            try:
                stored_path = Path(stored)
                if not stored_path.is_absolute() or ".." in stored_path.parts:
                    continue
                if not _same_checked_directory(stored_path, expected):
                    continue
            except (AccountConflict, OSError, RuntimeError, ValueError):
                continue
            found[item["accountId"]] = {
                "accountId": item["accountId"], "account": account,
                "workdir": str(expected),
                "wechatId": item.get("wechatId") if isinstance(item.get("wechatId"), str) else "",
                "nickname": item.get("nickname") if isinstance(item.get("nickname"), str) else "",
            }
        return found

    def _result_accounts(self):
        if not self.data_dir.is_dir():
            return []
        accounts = []
        for path in self.data_dir.glob("*.sqlite3"):
            if not ACCOUNT_ID.fullmatch(path.stem) or _reparse(path) or not path.is_file():
                continue
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                    rows = conn.execute("SELECT DISTINCT account FROM results_v2 LIMIT 2").fetchall()
            except sqlite3.DatabaseError:
                continue
            if len(rows) == 1 and _safe_account(rows[0][0]) and account_id(rows[0][0]) == path.stem:
                accounts.append(rows[0][0])
        return accounts

    @staticmethod
    def _upstream_wxid(account):
        return re.sub(r"_\w{4}$", "", account)

    def _discover(self):
        records = self._read_registry()
        candidates = self._result_accounts()
        _check_root(self.snapshot_root)
        if self.snapshot_root.is_dir():
            for workdir in self.snapshot_root.iterdir():
                owned = self.data_dir / (account_id(workdir.name) + ".sqlite3")
                if (_safe_account(workdir.name) and owned.is_file() and not _reparse(workdir) and workdir.is_dir() and
                        self._cache_files(workdir.name)):
                    candidates.append(workdir.name)
        for account in candidates:
            identifier = account_id(account)
            records.setdefault(identifier, {
                "accountId": identifier, "account": account,
                "workdir": str(self._workdir(account)),
                "wechatId": self._upstream_wxid(account), "nickname": "",
            })
        return records

    def _write_registry(self, records):
        _check_root(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if _regular(self.registry):
            pass
        temporary = self.data_dir / ("accounts.json.tmp-" + uuid.uuid4().hex)
        payload = {"version": 1, "accounts": sorted(records.values(), key=lambda item: item["accountId"])}
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, self.registry)
        finally:
            temporary.unlink(missing_ok=True)

    def register(self, account, workdir, wechat_id="", nickname=""):
        with self.lock:
            expected = self._workdir(account)
            provided = Path(os.path.abspath(workdir))
            if not _same_checked_directory(provided, expected):
                raise AccountConflict("账号缓存路径不安全")
            records = self._discover()
            identifier = account_id(account)
            item = {"accountId": identifier, "account": account, "workdir": str(expected),
                    "wechatId": wechat_id or self._upstream_wxid(account), "nickname": nickname or ""}
            if records.get(identifier) != item:
                records[identifier] = item
                self._write_registry(records)
            return identifier

    def list(self, current_account=None):
        with self.lock:
            records = self._discover()
            current_id = account_id(current_account) if current_account else None
            items = []
            for identifier, item in records.items():
                result = self.data_dir / (identifier + ".sqlite3")
                size = result.stat().st_size if _regular(result) else 0
                size += sum(path.stat().st_size for path in self._cache_files(item["account"]))
                items.append({"accountId": identifier, "wechatId": item["wechatId"],
                              "nickname": item["nickname"], "current": identifier == current_id,
                              "bytes": size})
            items.sort(key=lambda item: (not item["current"], item["wechatId"], item["accountId"]))
            return {"accounts": items, "currentAccountId": current_id}

    def resolve(self, identifier):
        if not ACCOUNT_ID.fullmatch(identifier):
            raise ValueError("invalid accountId")
        with self.lock:
            item = self._discover().get(identifier)
            if item is None:
                raise AccountNotFound("账号不存在")
            return dict(item)

    def delete(self, identifier, *, guard, forget):
        if not ACCOUNT_ID.fullmatch(identifier):
            raise ValueError("invalid accountId")
        with self.lock:
            records = self._discover()
            item = records.get(identifier)
            if item is None:
                raise AccountNotFound("账号不存在")
            account = item["account"]
            workdir = self._workdir(account)
            result = self.data_dir / (identifier + ".sqlite3")
            guard(account)
            if not forget(workdir):
                raise AccountConflict("账号快照正在刷新，请稍后重试")
            files, directories = self._owned_tree(account)
            if _regular(result):
                files.append(result)
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(result) + suffix)
                if _regular(sidecar):
                    files.append(sidecar)
            stable = self._stable_key_file(account)
            if stable is not None and _regular(stable):
                files.append(stable)
            for path in files:
                _check_root(path.parent)
                if _regular(path):
                    guard(account)
                    path.unlink()
            for directory in directories:
                if directory.is_dir() and not any(directory.iterdir()):
                    guard(account)
                    directory.rmdir()
            records.pop(identifier)
            self._write_registry(records)
            return {"deleted": identifier}

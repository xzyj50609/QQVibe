"""Dynamic WeChat discovery: data roots, accounts, shards, process, and bounded key candidates.

Read-only and bounded. Nothing writes to WeChat, escalates privileges, or scans unrelated
processes. Paths, usernames and keys never leave this module as text.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from ctypes import wintypes

from . import crypto, errors

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

MAX_SCAN_BYTES = 512 * 1024 * 1024
READ_CHUNK = 8 * 1024 * 1024
SCAN_DEADLINE_CHUNKS = 4096

HEX64 = re.compile(rb"[0-9a-fA-F]{64}")
# WCDB caches the derived raw key as an ASCII literal x'<hex>'. Real builds use 96 hex chars
# (64 enc_key + 32 salt); some use 64 (enc_key only) or an extended >96 form. Accept 64..192 so
# no valid layout is silently dropped.
SQL_KEY_LITERAL = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
CONFIG_READ_LIMIT = 64 * 1024
MAX_CONFIG_ENTRIES_PER_DIR = 64
MAX_REGISTRY_VALUES_PER_KEY = 128
CONFIG_FILE_SUFFIXES = frozenset((".ini", ".json", ".txt", ".cfg"))
CONFIG_PATH_KEYS = (
    "dataDir", "data_dir", "fileSavePath", "savePath", "path", "defaultFileSavePath"
)
DATA_ROOT_NAMES = ("xwechat_files", "WeChat Files", "xwechat_files_data")
TEXT_CONFIG_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\x00-\x1f\"']+")


@dataclass
class AccountDir:
    id: str
    path: str
    state: str
    message: str = ""


@dataclass
class ShardFile:
    id: str
    path: str
    wal_path: str | None = None


@dataclass
class WeixinProcess:
    pid: int
    exe: str | None = None


@dataclass
class KeyCandidate:
    key: bytes
    salt: bytes
    origin: str


def opaque_account_id(path: str) -> str:
    """Opaque, stable account id derived from the account path (never the plaintext dir name)."""
    return "acc-" + hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]


def find_weixin_processes() -> list[WeixinProcess]:
    if psutil is None:
        return []
    found: list[WeixinProcess] = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (process.info.get("name") or "").lower()
            if name in ("weixin.exe", "wechat.exe"):
                found.append(WeixinProcess(pid=int(process.info["pid"]), exe=process.info.get("exe")))
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover
            continue
    return found


def process_open_file_paths(pid: int) -> list[str]:
    """Read-only open-file path metadata for one process.

    Only paths are returned; no login database, credentials or file contents are read. Any psutil
    failure (process gone, access denied, non-Windows) yields an empty list.
    """
    if psutil is None:
        return []
    try:
        process = psutil.Process(pid)
        return [entry.path for entry in process.open_files() if entry.path]
    except Exception:  # psutil.NoSuchProcess / AccessDenied / OSError and similar
        return []


def _is_under(path: str, root: str) -> bool:
    """True when `path` is `root` or a descendant of `root`, on a normalized path boundary.

    Both sides are resolved (`realpath`) so a data root reached through a junction/symlink (the
    real xwechat layout: config points at ``D:\\微信聊天记录`` while WeChat opens files under the
    junction target) still matches the account directory.
    """
    normalized = os.path.normcase(os.path.realpath(path))
    boundary = os.path.normcase(os.path.realpath(root))
    return normalized == boundary or normalized.startswith(boundary + os.sep)


def active_account_id(
    account_dirs: list[AccountDir], processes: list[WeixinProcess]
) -> str | None:
    """Unique account whose db_storage contains an open file of a discovered Weixin process.

    Uses only open-file path metadata. Returns None for zero or more than one match so the caller
    never silently guesses the active account from mtime, nickname or login data.
    """
    if not account_dirs or not processes:
        return None
    matches: set[str] = set()
    for process in processes:
        for path in process_open_file_paths(process.pid):
            for account in account_dirs:
                if _is_under(path, account.path):
                    matches.add(account.id)
    return next(iter(matches)) if len(matches) == 1 else None


def _decode_config_bytes(data: bytes) -> str:
    """Decode a small config file, including legacy GBK paths (bounded, never logged)."""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            return data.decode("utf-8", "replace")


def _existing_absolute_directory(value: str) -> str | None:
    """Accept only an existing absolute directory, never a relative config value."""
    candidate = value.strip().strip("\ufeff").strip().strip('"').strip("'")
    if not candidate or not os.path.isabs(candidate) or not os.path.isdir(candidate):
        return None
    return os.path.abspath(candidate)


def _config_line_directory(line: str) -> str | None:
    """Extract an existing absolute directory from one config line.

    Accepts a bare absolute directory line (the real xwechat form) as well as a ``key=value``
    line; the value may itself be the data root or the parent of an ``xwechat_files`` directory.
    Anything that is not an existing absolute directory is ignored.
    """
    stripped = line.strip().strip("\ufeff").strip()
    if not stripped:
        return None
    direct = _existing_absolute_directory(stripped)
    if direct:
        return direct
    for separator in ("=", ":"):
        if separator in stripped:
            _, _, value = stripped.partition(separator)
            candidate = _existing_absolute_directory(value)
            if candidate:
                return candidate
    return None


def _config_content_directories(content: str) -> list[str]:
    """Extract upstream-supported JSON, plain-path and text config values."""
    roots: list[str] = []
    try:
        parsed = json.loads(content.strip().lstrip("\ufeff"))
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        values = (parsed.get(key) for key in CONFIG_PATH_KEYS)
    elif isinstance(parsed, list):
        values = iter(parsed)
    elif isinstance(parsed, str):
        values = iter((parsed,))
    else:
        values = iter(())
    for value in values:
        if isinstance(value, str):
            candidate = _existing_absolute_directory(value)
            if candidate and candidate not in roots:
                roots.append(candidate)
    if isinstance(parsed, (dict, list, str)):
        return roots
    for line in content.splitlines():
        candidate = _config_line_directory(line)
        if candidate and candidate not in roots:
            roots.append(candidate)
        # Older WeChat config text can embed a drive path inside a setting line.
        match = TEXT_CONFIG_PATH.search(line)
        if match:
            candidate = _existing_absolute_directory(match.group(0).rstrip("\\/;,"))
            if candidate and candidate not in roots:
                roots.append(candidate)
    return roots


def _config_dir_roots() -> list[str]:
    """Read bounded config files in the six WeChat 4.x config locations."""
    roots: list[str] = []
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        for parts in (("Tencent", "xwechat"),
                      ("Tencent", "xwechat", "config"),
                      ("Tencent", "WeChat")):
            config_dir = os.path.join(base, *parts)
            if not os.path.isdir(config_dir):
                continue
            try:
                with os.scandir(config_dir) as entries:
                    candidates = sorted(
                        (entry for entry in entries
                         if os.path.splitext(entry.name)[1].lower() in CONFIG_FILE_SUFFIXES),
                        key=lambda entry: entry.name.lower(),
                    )[:MAX_CONFIG_ENTRIES_PER_DIR]
                    for entry in candidates:
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            with open(entry.path, "rb") as handle:
                                data = handle.read(CONFIG_READ_LIMIT)
                        except OSError:
                            continue
                        for candidate in _config_content_directories(_decode_config_bytes(data)):
                            if candidate not in roots:
                                roots.append(candidate)
            except OSError:
                continue
    return roots


def _registry_roots() -> list[str]:
    roots: list[str] = []
    try:
        import winreg  # type: ignore

        for subkey in (r"Software\Tencent\xwechat", r"Software\Tencent\xwechat\config",
                       r"Software\Tencent\WeChat", r"Software\Tencent\Weixin"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as handle:
                    for index in range(MAX_REGISTRY_VALUES_PER_KEY):
                        try:
                            name, value, _type = winreg.EnumValue(handle, index)
                        except OSError:
                            break
                        if isinstance(value, str) and (
                            any(token in name.lower() for token in ("path", "dir", "save"))
                            or "xwechat" in value.lower()
                        ):
                            candidate = _existing_absolute_directory(value)
                            if candidate and candidate not in roots:
                                roots.append(candidate)
            except OSError:
                continue
    except Exception:  # pragma: no cover - non-Windows
        pass
    return roots


def _known_roots() -> list[str]:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    bases = [*_config_dir_roots(), *_registry_roots(), os.path.join(home, "Documents"), home]
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        appdata = os.environ.get(env_name)
        if appdata:
            bases.extend((os.path.join(appdata, "Tencent", "xwechat"),
                          os.path.join(appdata, "Tencent", "WeChat")))
    roots: list[str] = []
    for base in bases:
        for value in (base, *(os.path.join(base, name) for name in DATA_ROOT_NAMES)):
            candidate = _existing_absolute_directory(value)
            if candidate and candidate not in roots:
                roots.append(candidate)
    return roots


def discover_account_dirs() -> list[AccountDir]:
    accounts: list[AccountDir] = []
    seen: set[str] = set()
    for root in _known_roots():
        if not os.path.isdir(root):
            continue
        for entry in _safe_listdir(root):
            storage = os.path.join(root, entry, "db_storage")
            if os.path.isdir(storage):
                key = os.path.abspath(storage)
                if key not in seen:
                    seen.add(key)
                    accounts.append(AccountDir(id=opaque_account_id(key), path=key, state="available"))
        direct = os.path.join(root, "db_storage")
        if os.path.isdir(direct):
            key = os.path.abspath(direct)
            if key not in seen:
                seen.add(key)
                accounts.append(AccountDir(id=opaque_account_id(key), path=key, state="available"))
    return accounts


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def discover_shards(account: AccountDir) -> list[ShardFile]:
    shards: list[ShardFile] = []
    for dirpath, _dirnames, filenames in os.walk(account.path):
        for name in filenames:
            if not name.lower().endswith(".db"):
                continue
            full = os.path.join(dirpath, name)
            wal = full + "-wal"
            shards.append(
                ShardFile(
                    id=os.path.relpath(full, account.path).replace(os.sep, "/"),
                    path=full,
                    wal_path=wal if os.path.exists(wal) else None,
                )
            )
    return shards


class _MemoryBasicInformation64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


def _configure_kernel32(kernel32) -> None:
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint64,
        ctypes.POINTER(_MemoryBasicInformation64),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL


def _read_process_regions(pid: int):
    """Yield committed readable memory chunks (bounded, cancellable via deadline)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not process:
        raise errors.ProtocolError(errors.PERMISSION_DENIED, "OpenProcess failed")
    try:
        address = 0
        scanned = 0
        chunks = 0
        info = _MemoryBasicInformation64()
        while scanned < MAX_SCAN_BYTES and chunks < SCAN_DEADLINE_CHUNKS:
            if not kernel32.VirtualQueryEx(process, address, ctypes.byref(info), ctypes.sizeof(info)):
                break
            size = int(info.RegionSize)
            if size <= 0:
                break
            readable = (
                info.State == MEM_COMMIT
                and not (info.Protect & PAGE_GUARD)
                and not (info.Protect & PAGE_NOACCESS)
            )
            if readable:
                offset = 0
                while (offset < size and scanned < MAX_SCAN_BYTES
                       and chunks < SCAN_DEADLINE_CHUNKS):
                    take = min(READ_CHUNK, size - offset, MAX_SCAN_BYTES - scanned)
                    buffer = ctypes.create_string_buffer(take)
                    read = ctypes.c_size_t(0)
                    if not kernel32.ReadProcessMemory(
                        process, address + offset, buffer, take, ctypes.byref(read)
                    ):
                        break
                    chunk = buffer.raw[: int(read.value)]
                    scanned += len(chunk)
                    chunks += 1
                    if chunk:
                        yield chunk
                    offset += take
            address += size
    finally:
        kernel32.CloseHandle(process)


def scan_key_candidates(
    pid: int,
    salts: set[bytes],
    page_one_ciphertext: dict[bytes, bytes],
    max_candidates: int = 64,
) -> list[KeyCandidate]:
    """Bounded scan; a candidate is accepted only if it cryptographically validates the real
    page-1 ciphertext for its matching salt. Keys are never logged.

    Handles the WCDB cached raw-key literal ``x'<hex>'`` in all real layouts:
      * 96 hex chars -> 64 enc_key + 32 embedded salt,
      * 64 hex chars -> enc_key only (tried against every known salt),
      * >96 hex chars -> first 64 enc_key + last 32 salt (extended form).
    A bare 64-hex run (no ``x'`` wrapper) is also tried against every salt.
    """
    if not salts:
        return []
    candidates: list[KeyCandidate] = []
    seen: set[tuple[bytes, bytes]] = set()

    def accept(raw: bytes, salt: bytes, origin: str) -> bool:
        if salt not in salts or len(raw) != crypto.KEY_SIZE:
            return False
        marker = (raw, salt)
        if marker in seen:
            return False
        page = page_one_ciphertext.get(salt, b"")
        if not page or crypto.validate_key(raw, salt, page) is None:
            return False
        seen.add(marker)
        candidates.append(KeyCandidate(key=raw, salt=salt, origin=origin))
        return True

    for chunk in _read_process_regions(pid):
        for match in SQL_KEY_LITERAL.finditer(chunk):
            text = match.group(1)
            length = len(text)
            if length == 96:
                accept(bytes.fromhex(text[:64].decode("ascii")), bytes.fromhex(text[64:].decode("ascii")), "sql_literal_96")
            elif length == 64:
                raw = bytes.fromhex(text.decode("ascii"))
                for salt in salts:
                    accept(raw, salt, "sql_literal_64")
            else:  # even, >96: first 64 key, last 32 salt
                accept(bytes.fromhex(text[:64].decode("ascii")), bytes.fromhex(text[-32:].decode("ascii")), "sql_literal_ext")
        for match in HEX64.finditer(chunk):
            raw = bytes.fromhex(match.group(0).decode("ascii"))
            for salt in salts:
                accept(raw, salt, "hex64")
        if len(candidates) >= max_candidates:
            break
    return candidates

"""Atomic admission for every installation process and updater handoff.

The OS lock covers each metadata transaction, not the long installation. A
claim stays owned by the updater PID + creation time while files are swapped.
All launchers, shells and bridges register before starting work; no window can
enter between the updater's peer check and its claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

# The bundled Windows Python uses an isolated ._pth and does not add the
# invoked script's directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil
from process_lock import ProcessLock, checked_path

CLAIM_ENV = "WECHATVIBE_UPDATE_CLAIM"


def user_gate_home():
    """Stable per-user namespace shared by desktop/cmd with different TEMP."""
    configured = os.environ.get("LOCALAPPDATA")
    if configured and Path(configured).is_absolute():
        return checked_path(Path(configured) / "WechatVibe" / "installation-gates")
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_LOCAL_APPDATA: Windows resolves the actual user's known folder,
        # independently of a caller's TEMP/TMP or absent LOCALAPPDATA variable.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x1C, None, 0, buffer) != 0:
            raise InstallationBusy("无法确定此用户的安装占用目录")
        if not buffer.value or not Path(buffer.value).is_absolute():
            raise InstallationBusy("无法确定此用户的安装占用目录")
        return checked_path(Path(buffer.value) / "WechatVibe" / "installation-gates")
    return checked_path(Path.home() / ".local" / "share" / "WechatVibe" / "installation-gates")


class InstallationBusy(RuntimeError):
    pass


def process_identity(pid):
    process = psutil.Process(int(pid))
    return {"pid": process.pid, "created": process.create_time()}


def process_matches(identity):
    try:
        return process_identity(identity["pid"]) == {key: identity[key] for key in ("pid", "created")}
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, KeyError, TypeError, ValueError):
        # Unknown ownership must block an update, never count as a dead process.
        return True


class InstallationGate:
    def __init__(self, root, *, directory=None):
        self.root = Path(root).resolve()
        digest = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()
        self.directory = (Path(directory) if directory is not None else
                          user_gate_home() / digest)
        self.state_path = self.directory / "state.json"
        self.identity = None

    def _lock(self):
        return ProcessLock(self.directory / "admission.lock").acquire(timeout=10)

    def _load(self):
        checked_path(self.state_path)
        if not self.state_path.exists():
            return {"schema": 1, "processes": [], "claim": None}
        try:
            if self.state_path.stat().st_size > 1024 * 1024:
                raise ValueError("oversized gate state")
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("schema") != 1 or not isinstance(state.get("processes"), list):
                raise ValueError("invalid gate state")
            state["processes"] = [entry for entry in state["processes"] if process_matches(entry)]
            if state.get("claim") is not None and not process_matches(state["claim"]):
                state["claim"] = None
            return state
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise InstallationBusy("安装占用记录不可用，请保留文件并重新启动客户端") from exc

    def _save(self, state):
        temporary = self.directory / ("state." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(state, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _authorized(claim, identity, token):
        if not isinstance(token, str) or not secrets.compare_digest(token, claim.get("token", "")):
            return False
        try:
            process = psutil.Process(identity["pid"])
            ancestry = [process, *process.parents()]
            return any(item.pid == claim["pid"] and item.create_time() == claim["created"] for item in ancestry)
        except (psutil.Error, KeyError):
            return False

    def register(self, *, role="bridge", pid=None, token=None):
        if role not in ("shell", "launcher", "bridge"):
            raise ValueError("invalid installation process role")
        identity = process_identity(pid if pid is not None else os.getpid())
        token = os.environ.get(CLAIM_ENV) if token is None else token
        with self._lock():
            state = self._load()
            claim = state.get("claim")
            if claim is not None and not self._authorized(claim, identity, token):
                raise InstallationBusy("此安装正在更新，请等待更新完成后再打开窗口")
            state["processes"] = [entry for entry in state["processes"] if entry["pid"] != identity["pid"]]
            state["processes"].append({**identity, "role": role, "profile": os.environ.get("WECHATVIBE_PROFILE") or ""})
            self._save(state)
        self.identity = identity
        return identity

    def check_update(self, parent_pid):
        """Friendly preflight; the helper still makes the atomic exclusive claim."""
        profile = os.environ.get("WECHATVIBE_PROFILE") or ""
        with self._lock():
            state = self._load()
            if state.get("claim") is not None or any(
                    entry["pid"] != parent_pid and not (entry.get("role") == "bridge" and
                                                        entry.get("profile") == profile)
                    for entry in state["processes"]):
                raise InstallationBusy("请先关闭此安装的其他窗口，再更新或回退")
            self._save(state)

    def close(self):
        if self.identity is None:
            return
        with self._lock():
            state = self._load()
            state["processes"] = [entry for entry in state["processes"] if
                                  any(entry.get(key) != self.identity[key] for key in ("pid", "created"))]
            self._save(state)
        self.identity = None

    def claim(self, *, owner_pid, parent_pid):
        owner = process_identity(owner_pid)
        try:
            parent = process_identity(parent_pid) if parent_pid is not None else None
        except psutil.NoSuchProcess:
            parent = None
        with self._lock():
            state = self._load()
            if state.get("claim") is not None:
                old = state["claim"]
                if all(old.get(key) == owner[key] for key in ("pid", "created")):
                    return old["token"]
                raise InstallationBusy("另一个更新操作正在进行")
            peers = [entry for entry in state["processes"] if entry["pid"] not in (owner_pid, parent_pid)]
            if peers:
                raise InstallationBusy("请先关闭此安装的其他窗口，再更新或回退")
            if parent is not None and not any(item.pid == owner_pid for item in psutil.Process(parent_pid).children()):
                # The updater may only exempt the shell that launched it.
                raise InstallationBusy("更新进程不属于当前窗口")
            token = secrets.token_hex(32)
            state["claim"] = {**owner, "token": token}
            self._save(state)
            return token

    def release_claim(self, *, owner_pid, token):
        owner = process_identity(owner_pid)
        with self._lock():
            state = self._load()
            claim = state.get("claim")
            if claim is not None:
                if (any(claim.get(key) != owner[key] for key in ("pid", "created")) or
                        not secrets.compare_digest(str(token), claim.get("token", ""))):
                    raise InstallationBusy("更新占用已变化")
                state["claim"] = None
                self._save(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("register", "check", "claim", "release"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--parent", type=int)
    parser.add_argument("--role", default="shell")
    args = parser.parse_args()
    gate = InstallationGate(args.root)
    try:
        if args.action == "register":
            gate.register(role=args.role, pid=args.pid)
            result = {"registered": True}
        elif args.action == "check":
            gate.check_update(args.pid)
            result = {"available": True}
        elif args.action == "claim":
            result = {"token": gate.claim(owner_pid=args.pid, parent_pid=args.parent)}
        else:
            gate.release_claim(owner_pid=args.pid, token=os.environ.get(CLAIM_ENV, ""))
            result = {"released": True}
        print(json.dumps(result))
        return 0
    except (InstallationBusy, psutil.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

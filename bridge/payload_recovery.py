"""Offline, explicit recovery of one account's pre-encryption analysis backup.

The default invocation only validates. --apply claims the entire installation,
retains the current SQLite state, then restores the verified snapshot. It never
removes the global key or format marker, or inspects original WeChat files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

# The bundled Windows Python uses an isolated ._pth and does not add the
# invoked script's directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from installation_gate import InstallationGate
from payload_crypto import PayloadCipher, PayloadUnreadable
from process_lock import checked_path


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _read_only(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)


def _copy_file(source, destination, *, replace=False):
    checked_path(destination)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with source.open("rb") as origin, temporary.open("xb") as output:
            shutil.copyfileobj(origin, output)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot(source, destination):
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    checked_path(destination)
    try:
        with closing(_read_only(source)) as origin, closing(sqlite3.connect(temporary)) as output:
            origin.backup(output)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def recover(root, database, *, apply=False, unprotect=None, gate=None):
    if not isinstance(database, str) or not re.fullmatch(r"[0-9a-f]{64}\.sqlite3", database):
        raise ValueError("请选择账号分析库的 64 位文件名.sqlite3")
    root = Path(root).resolve()
    from product_profile import current_product
    product=current_product()
    path = checked_path(product.state_dir("real-client-data",root) / database)
    backup, manifest = path.with_suffix(".pre-wv1.sqlite3"), path.with_suffix(".pre-wv1.json")
    key = checked_path(product.state_dir("real-client-runtime",root) / "analysis-key.bin")
    key_backup = checked_path(key.with_name("analysis-key.backup.bin"))
    undo = checked_path(path.with_suffix(".restore-undo.sqlite3"))
    undo_key = checked_path(path.with_suffix(".restore-undo.key.bin"))
    undo_manifest = checked_path(path.with_suffix(".restore-undo.json"))
    gate = gate or InstallationGate(root)
    token = gate.claim(owner_pid=os.getpid(), parent_pid=None) if apply else None
    try:
        checked_path(backup)
        checked_path(manifest)
        if manifest.stat().st_size > 16384:
            raise ValueError("invalid recovery manifest")
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(saved, dict) or saved.get("schema") != 1 or
                saved.get("database") != backup.name or saved.get("sha256") != digest(backup) or
                saved.get("keyBackup") != os.path.relpath(key_backup, path.parent) or
                saved.get("keySha256") != digest(key_backup)):
            raise ValueError("invalid recovery manifest")
        # Confirm this Windows account can recover the protected backup before
        # touching either current file. Missing/current corrupt keys are allowed.
        PayloadCipher(key_backup, unprotect=unprotect)._read_key()
        with closing(_read_only(backup)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("invalid SQLite recovery backup")
        result = {"database": database, "backup": backup.name, "undo": undo.name,
                  "applied": False, "containsLegacyPlaintext": True,
                  "note": "恢复到首次打开加密版时的备份；后续分析保留在撤销副本中，格式标记不会移除"}
        if not apply:
            return result
        if any(item.exists() for item in (undo, undo_key, undo_manifest)):
            raise ValueError("已有恢复撤销副本，请先保留并检查它，再进行另一轮恢复")
        _snapshot(path, undo)
        if key.exists():
            _copy_file(key, undo_key)
        undo_data = {"schema": 1, "database": undo.name, "sha256": digest(undo),
                     "key": undo_key.name if undo_key.exists() else None,
                     "keySha256": digest(undo_key) if undo_key.exists() else None}
        temporary = undo_manifest.with_name(undo_manifest.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(undo_data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, undo_manifest)
        finally:
            temporary.unlink(missing_ok=True)
        _copy_file(key_backup, key, replace=True)
        # SQLite's backup transaction handles journal/WAL state; no sidecar is
        # deleted and no file is replaced beneath an open SQLite connection.
        with closing(_read_only(backup)) as origin, closing(sqlite3.connect(path)) as destination:
            origin.backup(destination)
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
        result["applied"] = True
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise PayloadUnreadable("恢复未完成；现有文件和已生成的撤销副本均已保留，请检查备份清单") from exc
    finally:
        if token is not None:
            gate.release_claim(owner_pid=os.getpid(), token=token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true", help="关闭所有窗口后恢复，并保留当前数据的撤销副本")
    arguments = parser.parse_args()
    try:
        print(json.dumps(recover(arguments.root, arguments.database, apply=arguments.apply), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

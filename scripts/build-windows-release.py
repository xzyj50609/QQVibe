"""Build a reviewed, reproducible Windows update ZIP from electron-builder output.

Only a stable-version ``win-unpacked`` directory is accepted. This command
creates the ZIP; ``build-update-manifest.cjs`` signs it as a separate step.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path


EXTRACTOR_PATH = Path(__file__).with_name("real-client-update-extract.py")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from product_profile import current_product
PRODUCT = current_product()
EXTRACTOR_SPEC = importlib.util.spec_from_file_location("real_client_update_extract", EXTRACTOR_PATH)
if EXTRACTOR_SPEC is None or EXTRACTOR_SPEC.loader is None:
    raise RuntimeError("update extractor is unavailable")
extractor = importlib.util.module_from_spec(EXTRACTOR_SPEC)
EXTRACTOR_SPEC.loader.exec_module(extractor)

VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
MAX_ARCHIVE_BYTES = 2 * 1024**3  # Matches build-update-manifest.cjs.
MAX_MEMBERS = extractor.MAX_MEMBERS
MAX_EXPANDED_BYTES = extractor.MAX_EXPANDED_BYTES
MAX_FILE_BYTES = extractor.MAX_FILE_BYTES
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
CHUNK_SIZE = 1024 * 1024
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

FORBIDDEN_DIRS = {
    ".local", "qqvibedata", ".git", ".svn", ".env", "__pycache__", "cache", "caches", "logs", "log",
    "accounts", "account-cache", "account_cache", "account_caches", "account-data", "account_data",
    "chats", "chat-cache", "chat_cache", "chat_caches", "chat-data", "chat_data", "chat-history",
    "messages", "msg", "session", "sessions", "history", "database", "db",
    "user-data", "userdata", "screenshots", "screenshot", "screen-captures",
    "screencaps", "secrets", "credentials", "backups", "backup",
}
FORBIDDEN_FILES = {
    "auth", "auth.json", "authorization.json", "credentials.json", "credential.json",
    "secrets.json", "secret.json", "token.json", "tokens.json", "keys.json", "key.json",
    "env.json", "environment.json", ".env", ".envrc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "account.json", "accounts.json", "chat.json", "chats.json", "messages.json", "session.json", "sessions.json",
}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pub", ".p12", ".pfx", ".jks", ".kdbx", ".log"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".ico"}
SENSITIVE_STEM_RE = re.compile(r"(?:auth|authorization|credentials?|secrets?|tokens?|keys?|api[-_]?key|private[-_]?key)(?:[._-].*)?\Z")
PUBLIC_CODE_SUFFIXES = {".py", ".pyi", ".pyd", ".js", ".cjs", ".mjs", ".ts", ".mts", ".md"}
DATABASE_SUFFIX_RE = re.compile(r"\.(?:db|sqlite|sqlite3)(?:[-.](?:wal|shm|journal|bak|backup))?\Z")
ALLOWED_APP_IMAGES = {
    ("resources", "client", "docs", "public", "images", "chatbean-overview.png"),
    ("resources", "client", "docs", "public", "images", "chatbean-portrait.png"),
    ("resources", "client", "chatui", "assets", "wechatvibe-icon.png"),
    ("resources", "client", "chatui", "assets", "wechatvibe-icon.ico"),
    ("resources", "client", "chatui", "assets", "qqvibe-icon.png"),
    ("resources", "client", "chatui", "assets", "qqvibe-icon.ico"),
    ("resources", "client", "docs", "public", "images", "overview.png"),
    ("resources", "client", "docs", "public", "images", "single-chat.png"),
    ("resources", "client", "docs", "public", "images", "group-chat.png"),
    ("resources", "client", "docs", "public", "images", "settings.png"),
    ("resources", "client", "docs", "public", "images", "first-run.png"),
    ("resources", "client", "docs", "public", "images", "settings-connection.png"),
    ("resources", "client", "docs", "public", "images", "group-labels.png"),
}
DEPENDENCY_IMAGE_ROOTS = (
    ("resources", "client", "runtime", "python", "lib", "site-packages", "win32com"),
    ("resources", "client", "runtime", "python", "lib", "site-packages", "wechatauto"),
)
SDK_CODE_ROOTS = (
    ("resources", "client", "node_modules", "@anthropic-ai", "sdk"),
    ("resources", "client", "node_modules", "openai"),
)
SDK_CODE_DIRS = {"messages", "sessions", "credentials"}
SDK_CODE_SUFFIXES = {".js", ".mjs", ".ts", ".mts", ".map"}
# Keep the builder's preflight in lockstep with the receiving extractor.
REQUIRED_FILES = extractor.required_files(PRODUCT.product_name)
ALLOWED_PUBLIC_KEY = ("resources", "client", "scripts", "update-signing.pub")
ALLOWED_QQ_PUBLIC_KEY = ("resources", "client", "scripts", "qq-update-signing.pub")
ASAR_SCRIPTS = (
    "scripts/desktop-main.cjs",
    "scripts/product-identity.cjs",
    "scripts/product-identity.json",
    "scripts/real-client-shell.cjs",
    "scripts/real-client-preload.cjs",
    "scripts/real-client-recovery.cjs",
    "scripts/real-client-update.cjs",
    "scripts/real-client-update-proxy.cjs",
    "scripts/real-client-model.cjs",
    "scripts/real-client-update-controller.cjs",
    "scripts/real-client-update-helper.cjs",
    "scripts/update-signing.pub",
    "scripts/real-client-update-config.cjs",
    "scripts/qq-update-signing.pub",
)
ASAR_CHECK = r"""
const asar = require('@electron/asar');
const crypto = require('node:crypto');
const archive = process.argv[1];
const names = JSON.parse(process.argv[2]);
const metadata = JSON.parse(asar.extractFile(archive, 'package.json').toString('utf8'));
const scripts = {};
for (const name of names) {
  scripts[name] = crypto.createHash('sha256').update(asar.extractFile(archive, name)).digest('hex');
}
process.stdout.write(JSON.stringify({name: metadata.name, main: metadata.main,
  version: metadata.version, scripts}));
"""


def _check_stat(path: Path, info: os.stat_result, *, directory: bool) -> None:
    is_reparse = bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)
    if stat.S_ISLNK(info.st_mode) or is_reparse:
        raise ValueError(f"symlink or reparse point is forbidden: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError(f"special or unexpected filesystem entry: {path}")


def _check_private_path(relative: Path, *, directory: bool) -> None:
    parts = relative.parts
    lowered = tuple(part.casefold() for part in parts)
    undici_cache = (lowered[:6] == ("resources", "client", "node_modules", "undici", "lib", "cache") or
                    lowered[:7] == ("resources", "client", "node_modules", "undici", "lib", "web", "cache"))
    sdk_root = next((root for root in SDK_CODE_ROOTS if lowered[:len(root)] == root), None)
    sdk_code_path = bool(sdk_root and any(part in SDK_CODE_DIRS for part in lowered[len(sdk_root):]))
    forbidden_dirs = FORBIDDEN_DIRS - ({"cache"} if undici_cache else set())
    if sdk_code_path:
        forbidden_dirs -= SDK_CODE_DIRS
    if (any(part in forbidden_dirs or part.startswith(".env.") for part in lowered[:-1]) or
            (directory and (lowered[-1] in forbidden_dirs or lowered[-1].startswith(".env.")))):
        raise ValueError(f"private or generated directory is forbidden: {relative}")
    if directory:
        return
    if sdk_code_path and relative.suffix.casefold() not in SDK_CODE_SUFFIXES:
        raise ValueError(f"non-code file in SDK code directory is forbidden: {relative}")
    name = lowered[-1]
    if lowered in (ALLOWED_PUBLIC_KEY, ALLOWED_QQ_PUBLIC_KEY):
        return
    if (name in FORBIDDEN_FILES or name.startswith(".env.") or
            name.startswith(("screenshot", "screen-shot", "screen_capture", "screencap")) or
            DATABASE_SUFFIX_RE.search(name) or
            ".log." in name or Path(name).suffix in PRIVATE_SUFFIXES or
            (SENSITIVE_STEM_RE.fullmatch(name) and Path(name).suffix not in PUBLIC_CODE_SUFFIXES)):
        raise ValueError(f"private or generated file is forbidden: {relative}")
    if (Path(name).suffix in IMAGE_SUFFIXES and lowered not in ALLOWED_APP_IMAGES and
            not any(lowered[:len(root)] == root for root in DEPENDENCY_IMAGE_ROOTS)):
        raise ValueError(f"unreviewed image location is forbidden: {relative}")


def _scan(source: Path) -> list[tuple[Path, Path, bool, os.stat_result]]:
    if source.name != "win-unpacked":
        raise ValueError("input must be the electron-builder win-unpacked directory")
    root_info = source.lstat()
    _check_stat(source, root_info, directory=True)
    rows: list[tuple[Path, Path, bool, os.stat_result]] = []
    seen: set[str] = set()
    pending = [source]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            children = list(entries)
        for entry in children:
            path = Path(entry.path)
            # On Windows DirEntry.stat can report zero dev/inode, unlike lstat.
            info = path.lstat()
            directory = stat.S_ISDIR(info.st_mode)
            _check_stat(path, info, directory=directory)
            relative = path.relative_to(source)
            _check_private_path(relative, directory=directory)
            member = "win-unpacked/" + relative.as_posix() + ("/" if directory else "")
            parts = extractor._parts(member, directory)
            normalized = unicodedata.normalize("NFC", "/".join(parts)).casefold()
            if normalized in seen:
                raise ValueError(f"case-colliding package path: {relative}")
            seen.add(normalized)
            rows.append((path, relative, directory, info))
            if directory:
                pending.append(path)
            if len(rows) + 1 > MAX_MEMBERS:
                raise ValueError("package member count exceeds extractor limit")
    rows.sort(key=lambda row: (unicodedata.normalize("NFC", row[1].as_posix()).casefold(), row[1].as_posix()))
    expanded = sum(info.st_size for _, _, directory, info in rows if not directory)
    if expanded > MAX_EXPANDED_BYTES or any(info.st_size > MAX_FILE_BYTES for _, _, directory, info in rows if not directory):
        raise ValueError("package expanded size exceeds extractor limit")
    return rows


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            result.update(chunk)
    return result.hexdigest()


def _verify_asar(files: dict[str, tuple[Path, os.stat_result]], version: str,
                 node_exe: Path | None) -> None:
    node_source = node_exe or os.environ.get("WECHATVIBE_BUILD_NODE") or shutil.which("node")
    if not node_source or not Path(node_source).is_file():
        raise ValueError("Node.js is required to inspect packaged app.asar")
    for name in ASAR_SCRIPTS:
        client_name = "resources/client/" + name
        if client_name not in files or files[client_name][1].st_size == 0:
            raise ValueError(f"packaged runtime file missing or empty: {client_name}")
    completed = subprocess.run(
        [str(node_source), "-e", ASAR_CHECK, str(files["resources/app.asar"][0]),
         json.dumps(ASAR_SCRIPTS)], cwd=ROOT, capture_output=True, text=True,
    )
    if completed.returncode:
        raise ValueError("app.asar inspection failed; check @electron/asar and archive integrity")
    report = json.loads(completed.stdout)
    if (report.get("name") != PRODUCT.product_name.lower() or report.get("main") != "scripts/desktop-main.cjs" or
            report.get("version") != version):
        raise ValueError("app.asar identity does not match requested stable version")
    for name in ASAR_SCRIPTS:
        if report["scripts"].get(name) != _digest(files["resources/client/" + name][0]):
            raise ValueError(f"app.asar script differs from packaged client: {name}")


def _require_package(rows: list[tuple[Path, Path, bool, os.stat_result]], version: str,
                     node_exe: Path | None) -> None:
    files = {relative.as_posix(): (path, info) for path, relative, directory, info in rows if not directory}
    for name in REQUIRED_FILES:
        if name not in files or files[name][1].st_size == 0:
            raise ValueError(f"packaged runtime file missing or empty: {name}")
    for public_key_name in ("/".join(ALLOWED_PUBLIC_KEY), "/".join(ALLOWED_QQ_PUBLIC_KEY)):
        if public_key_name not in files:
            continue
        public_key, public_key_info = files[public_key_name]
        if public_key_info.st_size > 16 * 1024:
            raise ValueError("packaged public signing key is too large")
        public_key_bytes = public_key.read_bytes().strip()
        if not (public_key_bytes.startswith(b"-----BEGIN PUBLIC KEY-----") and
                public_key_bytes.endswith(b"-----END PUBLIC KEY-----") and
                b"PRIVATE KEY" not in public_key_bytes):
            raise ValueError("packaged public signing key is invalid")
    metadata, metadata_info = files["resources/client/package.json"]
    if metadata_info.st_size > 64 * 1024:
        raise ValueError("packaged runtime metadata is too large")
    package = json.loads(metadata.read_text(encoding="utf-8"))
    if package.get("name") != PRODUCT.product_name.lower() + "-runtime" or package.get("version") != version:
        raise ValueError("packaged runtime version does not match requested stable version")
    _verify_asar(files, version, node_exe)
    with files[PRODUCT.product_name + ".exe"][0].open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("packaged executable header is invalid")


def _zip_info(name: str, directory: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIME)
    info.create_system = 0  # Stable Windows attributes on every host.
    info.compress_type = zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
    info.external_attr = 0x10 if directory else 0x20
    return info


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)


def _same_scan(before: list[tuple[Path, Path, bool, os.stat_result]],
               after: list[tuple[Path, Path, bool, os.stat_result]]) -> bool:
    return len(before) == len(after) and all(
        old_relative == new_relative and old_directory == new_directory and _same_file(old_info, new_info)
        for (_, old_relative, old_directory, old_info), (_, new_relative, new_directory, new_info)
        in zip(before, after)
    )


def _write_archive(temp: Path, rows: list[tuple[Path, Path, bool, os.stat_result]]) -> None:
    with zipfile.ZipFile(temp, "w", allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(_zip_info("win-unpacked/", True), b"")
        for path, relative, directory, before in rows:
            name = "win-unpacked/" + relative.as_posix() + ("/" if directory else "")
            info = _zip_info(name, directory)
            latest = path.lstat()
            _check_stat(path, latest, directory=directory)
            if not _same_file(before, latest):
                raise ValueError(f"package changed during build: {relative}")
            if directory:
                archive.writestr(info, b"")
                continue
            with path.open("rb") as input_file:
                opened = os.fstat(input_file.fileno())
                if not _same_file(before, opened):
                    raise ValueError(f"package changed during build: {relative}")
                with archive.open(info, "w", force_zip64=True) as output_file:
                    copied = 0
                    while chunk := input_file.read(CHUNK_SIZE):
                        copied += len(chunk)
                        if copied > before.st_size:
                            raise ValueError(f"package changed during build: {relative}")
                        output_file.write(chunk)
                    if copied != before.st_size:
                        raise ValueError(f"package changed during build: {relative}")
            if not _same_file(before, path.lstat()):
                raise ValueError(f"package changed during build: {relative}")


def build_release(source: Path, output_dir: Path, version: str,
                  node_exe: Path | None = None, *, with_model: bool = False,
                  internal_preview: bool = False) -> Path:
    if not isinstance(version, str) or len(version) > 80 or not VERSION_RE.fullmatch(version):
        raise ValueError("version must be stable SemVer X.Y.Z")
    source = Path(source).absolute()
    output_dir = Path(output_dir).absolute()
    resolved_source = source.resolve(strict=True)
    resolved_output = output_dir.resolve(strict=False)
    if resolved_output == resolved_source or resolved_source in resolved_output.parents:
        raise ValueError("output directory cannot be inside package input")
    rows = _scan(source)
    _require_package(rows, version, node_exe)
    included = (rows if with_model else
                [row for row in rows if row[1].parts[:3] != ("resources", "client", ".models")])
    output_dir.mkdir(parents=True, exist_ok=True)
    _check_stat(output_dir, output_dir.lstat(), directory=True)
    suffix = "-windows-x64-full.zip" if with_model else "-windows-x64.zip"
    if internal_preview:
        suffix = suffix.replace("-windows-x64", "-windows-x64-internal-preview")
    target = output_dir / f"{PRODUCT.product_name}-{version}{suffix}"
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"release archive already exists: {target}")
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=output_dir)
    os.close(handle)
    temp = Path(temp_name)
    try:
        _write_archive(temp, included)
        if temp.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("compressed archive exceeds 2 GiB signing limit")
        with zipfile.ZipFile(temp) as archive:
            inspected = extractor.inspect(archive, requirements=REQUIRED_FILES)
            if len(inspected) != len(included) + 1:
                raise ValueError("archive member count changed during build")
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"archive CRC check failed: {bad}")
        if not _same_scan(rows, _scan(source)):
            raise ValueError("package changed during build")
        # Hard-linking the completed temp file publishes it atomically without overwriting.
        os.link(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="electron-builder win-unpacked directory")
    parser.add_argument("--version", required=True, help="stable X.Y.Z version")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for the unsigned release ZIP")
    parser.add_argument("--node-exe", type=Path, help="Node executable with project @electron/asar installed")
    parser.add_argument("--with-model", action="store_true",
                        help="include the Laya model in a separate -windows-x64-full.zip archive")
    parser.add_argument("--internal-preview", action="store_true", help="label the ZIP as an internal preview")
    args = parser.parse_args()
    try:
        archive = build_release(args.input, args.output_dir, args.version, args.node_exe,
                                with_model=args.with_model, internal_preview=args.internal_preview)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, RuntimeError) as error:
        parser.exit(1, f"Windows release build rejected: {error}\n")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "version": args.version}, ensure_ascii=True))


if __name__ == "__main__":
    main()

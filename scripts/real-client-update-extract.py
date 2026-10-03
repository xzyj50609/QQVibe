"""Extract a verified WechatVibe update ZIP into a new, bounded candidate tree.

The caller verifies the signed manifest and archive SHA-256 first. This helper
enforces filesystem safety before it writes any member of the archive.
"""
from __future__ import annotations

import argparse
import json
import re
import stat
import sys
import unicodedata
import zipfile
from pathlib import Path

MAX_MEMBERS = 20_000
MAX_EXPANDED_BYTES = 4 * 1024**3
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_RATIO = 100
# Neither product's local user data may arrive inside an update package: a directory
# named like the other product's data root would be silently adopted as our own.
RESERVED_DATA_DIRECTORIES = frozenset({".local", "qqvibedata"})
MAX_FILE_RATIO = 1_000
CHUNK_SIZE = 1024 * 1024
BAD_WINDOWS_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                          *(f"LPT{i}" for i in range(1, 10))}
REQUIRED_FILES = (
    "WechatVibe.exe",
    "resources/app.asar",
    "resources/client/package.json",
    "resources/client/runtime/python/python.exe",
    "resources/client/runtime/node/node.exe",
    "resources/client/scripts/start-real-client.py",
    "resources/client/scripts/real-client-update-helper.cjs",
    "resources/client/scripts/real-client-update-extract.py",
    "resources/client/scripts/update-signing.pub",
    "resources/client/bridge/chat_server.py",
    "resources/client/chatui/index.html",
)


def _parts(name: str, is_dir: bool) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or BAD_WINDOWS_CHARS.search(name):
        raise ValueError("unsafe ZIP member path")
    if is_dir:
        if not name.endswith("/"):
            raise ValueError("directory member lacks trailing slash")
        name = name[:-1]
    elif name.endswith("/"):
        raise ValueError("file member has trailing slash")
    parts = tuple(name.split("/"))
    if not parts or len(name) > 1024 or parts[0] != "win-unpacked" or any(
        not part or len(part) > 255 or part in (".", "..") or part.endswith((" ", ".")) or
        part.casefold() in RESERVED_DATA_DIRECTORIES or
        part.split(".", 1)[0].rstrip(" ").upper() in RESERVED_WINDOWS_NAMES
        for part in parts
    ):
        raise ValueError("unsafe ZIP member path")
    return parts


def _member_kind(info: zipfile.ZipInfo) -> bool:
    is_dir = info.is_dir()
    mode = (info.external_attr >> 16) & 0xFFFF
    if (info.external_attr & 0xFFFF) & 0x400 or mode & 0x400:
        raise ValueError("ZIP reparse point is forbidden")
    if info.create_system == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG):
        raise ValueError("ZIP special file is forbidden")
    return is_dir


def required_files(product_name="WechatVibe"):
    if not isinstance(product_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", product_name):
        raise ValueError("invalid product name")
    extras = ("resources/client/scripts/real-client-update-config.cjs",
              "resources/client/scripts/qq-update-signing.pub",
              "resources/client/scripts/product-identity.json") if product_name == "QQVibe" else ()
    return (product_name + ".exe", *REQUIRED_FILES[1:], *extras)


def inspect(archive: zipfile.ZipFile, *, requirements=None) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_MEMBERS:
        raise ValueError("ZIP member count exceeds limit")
    seen: dict[str, str] = {}
    files: dict[str, zipfile.ZipInfo] = {}
    rows = []
    expanded = compressed = 0
    for info in entries:
        is_dir = _member_kind(info)
        parts = _parts(info.filename, is_dir)
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if key in seen:
            raise ValueError("duplicate or case-colliding ZIP member")
        seen[key] = "dir" if is_dir else "file"
        if info.flag_bits & 0x1:
            raise ValueError("encrypted ZIP member is forbidden")
        if info.file_size < 0 or info.compress_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise ValueError("ZIP member exceeds size limit")
        if not is_dir:
            expanded += info.file_size
            compressed += info.compress_size
            files["/".join(parts)] = info
            if info.file_size and info.file_size > max(info.compress_size, 1) * MAX_FILE_RATIO:
                raise ValueError("ZIP member compression ratio exceeds limit")
        elif info.file_size:
            raise ValueError("directory ZIP member has content")
        rows.append((info, parts, is_dir))
    if expanded > MAX_EXPANDED_BYTES or expanded > max(compressed, 1) * MAX_TOTAL_RATIO:
        raise ValueError("ZIP expanded size or compression ratio exceeds limit")
    for relative in REQUIRED_FILES if requirements is None else requirements:
        member = files.get("win-unpacked/" + relative)
        if member is None or member.file_size == 0:
            raise ValueError("candidate required file missing or empty: " + relative)
    for key, kind in seen.items():
        ancestors = key.split("/")[:-1]
        for index in range(1, len(ancestors) + 1):
            if seen.get("/".join(ancestors[:index])) == "file":
                raise ValueError("ZIP path has a file as parent")
    return rows


def extract(archive_path: Path, work_dir: Path, expected_version: str, *, product_name="WechatVibe") -> Path:
    requirements = required_files(product_name)
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", expected_version):
        raise ValueError("expected version must be stable SemVer")
    archive_path = archive_path.resolve(strict=True)
    work_dir = work_dir.resolve(strict=True)
    if not work_dir.is_dir() or archive_path.parent != work_dir:
        raise ValueError("archive must be inside work directory")
    candidate = work_dir / "win-unpacked"
    if candidate.exists() or candidate.is_symlink():
        raise ValueError("candidate already exists")
    with zipfile.ZipFile(archive_path) as archive:
        rows = inspect(archive, requirements=requirements)
        written = 0
        for info, parts, is_dir in rows:
            target = work_dir.joinpath(*parts)
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as destination:
                copied = 0
                while chunk := source.read(CHUNK_SIZE):
                    copied += len(chunk)
                    written += len(chunk)
                    if copied > info.file_size or written > MAX_EXPANDED_BYTES:
                        raise ValueError("ZIP extraction exceeded declared size")
                    destination.write(chunk)
                if copied != info.file_size:
                    raise ValueError("ZIP member size mismatch")
    for relative in requirements:
        target = candidate / relative
        attributes = target.lstat()
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_size == 0 or (
            getattr(attributes, "st_file_attributes", 0) &
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError("candidate required file unsafe: " + relative)
    executable = candidate / (product_name + ".exe")
    metadata = candidate / "resources" / "client" / "package.json"
    if not executable.is_file() or executable.is_symlink() or executable.stat().st_size == 0:
        raise ValueError("candidate executable missing")
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("candidate executable header invalid")
    if not metadata.is_file() or metadata.is_symlink() or metadata.stat().st_size > 64 * 1024:
        raise ValueError("candidate package metadata missing")
    package = json.loads(metadata.read_text(encoding="utf-8"))
    if package.get("name") != product_name.lower() + "-runtime" or package.get("version") != expected_version:
        raise ValueError("candidate package version mismatch")
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("expected_version")
    parser.add_argument("--product-name", choices=("WechatVibe", "QQVibe"), default="WechatVibe")
    args = parser.parse_args()
    try:
        candidate = extract(args.archive, args.work_dir, args.expected_version, product_name=args.product_name)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, json.JSONDecodeError) as error:
        print(f"update extraction rejected: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps({"candidatePath": str(candidate)}, ensure_ascii=True))


if __name__ == "__main__":
    main()

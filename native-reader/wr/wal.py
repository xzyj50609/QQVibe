"""SQLite WAL parsing with committed-frame semantics.

Only frames that pass salt + rolling-checksum validation up to the last commit frame are returned;
uncommitted frames are never published. SQLCipher-encrypted WAL pages use the main database's key
and salt (no per-frame salt prefix).
"""

from __future__ import annotations

from dataclasses import dataclass

# SQLite WAL magic: 0x377f0682 => checksums use the platform (little) byte order;
# 0x377f0683 => byte-swapped (big). Confirmed against stdlib-SQLite-generated WAL.
WAL_MAGIC_LITTLE = 0x377F0682
WAL_MAGIC_BIG = 0x377F0683
WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24
VALID_PAGE_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)


@dataclass(frozen=True)
class WalFrame:
    page_number: int
    page: bytes


@dataclass(frozen=True)
class WalParseResult:
    frames: list["WalFrame"]
    header_ok: bool
    corrupt_before_commit: bool
    commit_db_size: int | None = None


def parse_wal(data: bytes) -> WalParseResult:
    """Like parse_committed_frames but distinguishes an invalid header / corrupt frame before any
    valid commit (explicit failure) from a benign partial/uncommitted tail (keeps the commit).
    Also reports the committed database size in pages (the last non-zero dbSize frame)."""
    frames, commit_db_size = _parse_committed(data)
    if len(data) < WAL_HEADER_SIZE:
        return WalParseResult(frames, header_ok=False, corrupt_before_commit=False, commit_db_size=commit_db_size)
    magic = int.from_bytes(data[0:4], "big")
    header_ok = magic in (WAL_MAGIC_LITTLE, WAL_MAGIC_BIG)
    corrupt_before_commit = (not header_ok or _has_corruption_before_commit(data)) and not frames
    return WalParseResult(
        frames,
        header_ok=header_ok,
        corrupt_before_commit=corrupt_before_commit,
        commit_db_size=commit_db_size,
    )


def _has_corruption_before_commit(data: bytes) -> bool:
    """True when the first frame is present but fails salt/checksum validation."""
    if len(data) < WAL_HEADER_SIZE:
        return False
    magic = int.from_bytes(data[0:4], "big")
    if magic not in (WAL_MAGIC_LITTLE, WAL_MAGIC_BIG):
        return False
    little_endian = magic == WAL_MAGIC_LITTLE
    page_size = int.from_bytes(data[8:12], "big")
    if page_size not in VALID_PAGE_SIZES:
        return False
    salt1, salt2 = data[16:20], data[20:24]
    s0 = int.from_bytes(data[24:28], "big")
    s1 = int.from_bytes(data[28:32], "big")
    if (s0, s1) != _checksum(data[0:24], 0, 0, little_endian):
        return True
    if len(data) < WAL_HEADER_SIZE + FRAME_HEADER_SIZE + page_size:
        return False
    frame_header = data[WAL_HEADER_SIZE : WAL_HEADER_SIZE + FRAME_HEADER_SIZE]
    page = data[WAL_HEADER_SIZE + FRAME_HEADER_SIZE : WAL_HEADER_SIZE + FRAME_HEADER_SIZE + page_size]
    next0, next1 = _checksum(frame_header[0:8] + page, s0, s1, little_endian)
    return frame_header[8:12] != salt1 or frame_header[12:16] != salt2 or (next0, next1) != (
        int.from_bytes(frame_header[16:20], "big"),
        int.from_bytes(frame_header[20:24], "big"),
    )


def _checksum(data: bytes, s0: int, s1: int, little_endian: bool) -> tuple[int, int]:
    order = "little" if little_endian else "big"
    for offset in range(0, len(data) - 7, 8):
        x0 = int.from_bytes(data[offset : offset + 4], order)
        x1 = int.from_bytes(data[offset + 4 : offset + 8], order)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def parse_committed_frames(data: bytes) -> list[WalFrame]:
    """Return only the frames belonging to the last valid commit, in order."""
    return _parse_committed(data)[0]


def _parse_committed(data: bytes) -> tuple[list[WalFrame], int | None]:
    """Return (last-commit frames, committed page count) for a SQLite WAL image."""
    if len(data) < WAL_HEADER_SIZE:
        return [], None
    magic = int.from_bytes(data[0:4], "big")
    if magic not in (WAL_MAGIC_LITTLE, WAL_MAGIC_BIG):
        return [], None
    little_endian = magic == WAL_MAGIC_LITTLE
    page_size = int.from_bytes(data[8:12], "big")
    if page_size not in VALID_PAGE_SIZES:
        return [], None

    salt1 = data[16:20]
    salt2 = data[20:24]
    s0 = int.from_bytes(data[24:28], "big")
    s1 = int.from_bytes(data[28:32], "big")
    header_s0, header_s1 = _checksum(data[0:24], 0, 0, little_endian)
    if (header_s0, header_s1) != (s0, s1):
        return [], None

    frames: list[WalFrame] = []
    committed: list[WalFrame] = []
    commit_size: int | None = None
    offset = WAL_HEADER_SIZE
    while offset + FRAME_HEADER_SIZE + page_size <= len(data):
        frame_header = data[offset : offset + FRAME_HEADER_SIZE]
        page_number = int.from_bytes(frame_header[0:4], "big")
        db_size_after = int.from_bytes(frame_header[4:8], "big")
        frame_salt1 = frame_header[8:12]
        frame_salt2 = frame_header[12:16]
        check0 = int.from_bytes(frame_header[16:20], "big")
        check1 = int.from_bytes(frame_header[20:24], "big")
        page = data[offset + FRAME_HEADER_SIZE : offset + FRAME_HEADER_SIZE + page_size]

        next0, next1 = _checksum(frame_header[0:8] + page, s0, s1, little_endian)
        if frame_salt1 != salt1 or frame_salt2 != salt2 or (next0, next1) != (check0, check1):
            break
        s0, s1 = next0, next1
        frames.append(WalFrame(page_number, page))
        if db_size_after != 0:
            committed = list(frames)
            commit_size = db_size_after
        offset += FRAME_HEADER_SIZE + page_size

    return committed, commit_size


def build_wal_header(page_size: int, salt1: bytes, salt2: bytes, little_endian: bool = False) -> bytes:
    """Build a WAL header with a correct checksum (synthetic tests / tooling)."""
    magic = WAL_MAGIC_LITTLE if little_endian else WAL_MAGIC_BIG
    base = (
        magic.to_bytes(4, "big")
        + (3007000).to_bytes(4, "big")
        + page_size.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + salt1
        + salt2
    )
    s0, s1 = _checksum(base, 0, 0, little_endian)
    return base + s0.to_bytes(4, "big") + s1.to_bytes(4, "big")


def build_frame(
    page_number: int,
    page: bytes,
    salt1: bytes,
    salt2: bytes,
    s0: int,
    s1: int,
    commit: bool,
    db_size_after: int,
    little_endian: bool = False,
) -> tuple[bytes, int, int]:
    """Build one WAL frame; returns (bytes, new_s0, new_s1).

    The rolling checksum covers only the first 8 header bytes (pgno, dbSize) plus the page data,
    matching SQLite's frame checksum (salt bytes are excluded).
    """
    prefix = page_number.to_bytes(4, "big") + (db_size_after if commit else 0).to_bytes(4, "big")
    next0, next1 = _checksum(prefix + page, s0, s1, little_endian)
    frame = prefix + salt1 + salt2 + next0.to_bytes(4, "big") + next1.to_bytes(4, "big") + page
    return frame, next0, next1

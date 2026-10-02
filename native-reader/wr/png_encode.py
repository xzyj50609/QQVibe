"""Minimal dependency-free PNG encoder for window-frame evidence.

Standard library only (``zlib`` + ``struct``). Encodes an 8-bit true-colour (RGB) buffer into a PNG
byte string so the reader can hand one bounded, in-memory frame to the Electron OCR step. No image is
ever written to disk and no third-party image library is used.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COLOUR_TYPE_RGB = 2
_BIT_DEPTH = 8


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def encode_png_rgb(width: int, height: int, rgb: bytes) -> bytes:
    """Encode a top-down RGB buffer (``width*height*3`` bytes) as a PNG.

    Raises ``ValueError`` on a malformed size/buffer instead of emitting a corrupt image.
    """
    if width <= 0 or height <= 0:
        raise ValueError("invalid png dimensions")
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError("rgb buffer size does not match dimensions")

    ihdr = struct.pack(">IIBBBBB", width, height, _BIT_DEPTH, _COLOUR_TYPE_RGB, 0, 0, 0)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) keeps the encoder tiny and correct
        start = y * stride
        raw += rgb[start : start + stride]

    idat = zlib.compress(bytes(raw), 6)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )

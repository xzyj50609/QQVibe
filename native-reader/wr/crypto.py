"""SQLCipher 4 page crypto (independently written).

Layout (SQLCipher 4 defaults): page size 4096, reserved 80 bytes = 16-byte IV + 64-byte HMAC.
AES-256-CBC over the ciphertext; HMAC-SHA512 with
``hmac_key = PBKDF2-HMAC-SHA512(key, salt XOR 0x3a, iterations=2, dklen=32)``.
The HMAC covers ``ciphertext || iv || page_number_le32``.

Page 1 of the main database carries the 16-byte salt in its first 16 bytes; the logical page then
re-prepends the constant ``b"SQLite format 3\\x00"`` header. Every page's HMAC is verified; a bad
page raises instead of silently decrypting with a wrong key.

This module performs no network, process, or UI access.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from . import errors

PAGE_SIZE = 4096
RESERVED = 80
IV_SIZE = 16
HMAC_SIZE = 64
KEY_SIZE = 32
SALT_SIZE = 16
SQLITE_HEADER = b"SQLite format 3\x00"
KDF_ITERATIONS = 256000


@dataclass(frozen=True)
class PageKey:
    """Derived key material for one database salt (all in memory only)."""

    key: bytes
    salt: bytes
    hmac_key: bytes


def _xor_mask(salt: bytes) -> bytes:
    return bytes(b ^ 0x3A for b in salt)


def derive_hmac_key(key: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA512(), length=32, salt=_xor_mask(salt), iterations=2)
    return kdf.derive(key)


def make_page_key(raw_key: bytes, salt: bytes) -> PageKey:
    if len(raw_key) != KEY_SIZE:
        raise errors.ProtocolError(errors.KEY_UNAVAILABLE, "raw key must be 32 bytes")
    if len(salt) != SALT_SIZE:
        raise errors.ProtocolError(errors.UNSUPPORTED_SCHEMA, "salt must be 16 bytes")
    return PageKey(key=raw_key, salt=salt, hmac_key=derive_hmac_key(raw_key, salt))


def derive_key_from_passphrase(passphrase: bytes, salt: bytes) -> bytes:
    """Explicit passphrase KDF (PBKDF2-HMAC-SHA512, 256000 iterations, 32-byte key)."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA512(), length=KEY_SIZE, salt=salt, iterations=KDF_ITERATIONS
    )
    return kdf.derive(passphrase)


def _expected_hmac(page_key: PageKey, ciphertext: bytes, iv: bytes, page_number: int) -> bytes:
    mac = hmac.new(page_key.hmac_key, digestmod=hashlib.sha512)
    mac.update(ciphertext)
    mac.update(iv)
    mac.update(struct.pack("<I", page_number))
    return mac.digest()


def decrypt_page(raw: bytes, page_key: PageKey, page_number: int, main_page_one: bool) -> bytes:
    """Decrypt one 4096-byte encrypted page into its usable plaintext (4016 bytes)."""
    if len(raw) != PAGE_SIZE:
        raise errors.ProtocolError(errors.READ_ERROR, "page is not 4096 bytes")
    offset = SALT_SIZE if main_page_one else 0
    if offset and raw[:SALT_SIZE] != page_key.salt:
        raise errors.ProtocolError(errors.READ_ERROR, "page-1 salt does not match key")
    body = raw[offset:]
    ciphertext = body[: PAGE_SIZE - offset - RESERVED]
    iv = body[PAGE_SIZE - offset - RESERVED : PAGE_SIZE - offset - RESERVED + IV_SIZE]
    stored = body[PAGE_SIZE - offset - RESERVED + IV_SIZE :]
    expected = _expected_hmac(page_key, ciphertext, iv, page_number)
    if not hmac.compare_digest(stored, expected):
        raise errors.ProtocolError(errors.KEY_UNAVAILABLE, "page HMAC mismatch")
    decryptor = Cipher(algorithms.AES(page_key.key), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    if main_page_one:
        plaintext = SQLITE_HEADER + plaintext
    return plaintext


def encrypt_page(usable: bytes, page_key: PageKey, page_number: int, main_page_one: bool) -> bytes:
    """Inverse of :func:`decrypt_page` (used by synthetic tests; same layout as real SQLCipher)."""
    if len(usable) != PAGE_SIZE - RESERVED:
        raise ValueError("usable page must be page_size - reserved")
    plaintext = usable[SALT_SIZE:] if main_page_one else usable
    iv = bytes(16)  # deterministic IV only for tests; real pages use random IVs
    encryptor = Cipher(algorithms.AES(page_key.key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    mac = _expected_hmac(page_key, ciphertext, iv, page_number)
    body = ciphertext + iv + mac
    if main_page_one:
        return page_key.salt + body
    return body


def is_valid_sqlite_page_one(usable: bytes) -> bool:
    """Structural check of a decrypted page 1 (page size + reserved byte)."""
    if len(usable) < 100 or usable[:16] != SQLITE_HEADER:
        return False
    page_size = struct.unpack(">H", usable[16:18])[0]
    reserved = usable[20]
    return page_size == PAGE_SIZE and reserved == RESERVED


def validate_key(raw_key: bytes, salt: bytes, page_one_ciphertext: bytes) -> PageKey | None:
    """Return the derived PageKey iff page 1 decrypts and validates, else None."""
    try:
        page_key = make_page_key(raw_key, salt)
        usable = decrypt_page(page_one_ciphertext, page_key, 1, main_page_one=True)
    except errors.ProtocolError:
        return None
    if not is_valid_sqlite_page_one(usable):
        return None
    return page_key


def looks_like_key(hex_text: str) -> bytes | None:
    """Parse a 64-hex-char raw key, or None."""
    if len(hex_text) != 64:
        return None
    try:
        return bytes.fromhex(hex_text)
    except ValueError:
        return None

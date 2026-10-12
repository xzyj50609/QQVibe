"""Authenticated encryption for analysis payload columns containing chat text.

Based on PR #33's AES-GCM envelope and DPAPI key design. Legacy JSON remains
readable and is encrypted on its next write. A missing, corrupt or foreign key
is an explicit error; it never means an empty analysis result. Advisor history
and other SQLite columns are outside this column-level format.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import threading
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from process_lock import ProcessLock, checked_path

ENVELOPE = "wv1:"
KEY_BYTES = 32
NONCE_BYTES = 12
KEY_FILENAME = "analysis-key.bin"


class PayloadUnreadable(RuntimeError):
    code = "analysis-data-unreadable"

    def __init__(self, message="本机无法解密已有分析数据，请恢复原账号的密钥备份；现有数据已保留"):
        super().__init__(message)


def _dpapi_protect(value):
    import win32crypt
    return win32crypt.CryptProtectData(value, "WechatVibe analysis key", None, None, None, 0x1)


def _dpapi_unprotect(value):
    import win32crypt
    return win32crypt.CryptUnprotectData(value, None, None, None, 0x1)[1]


class PayloadCipher:
    def __init__(self, key_path=None, *, protect=None, unprotect=None, key=None):
        self.key_path = Path(key_path) if key_path is not None else None
        self._protect, self._unprotect = protect or _dpapi_protect, unprotect or _dpapi_unprotect
        self.lock = threading.RLock()
        if key is not None and (not isinstance(key, bytes) or len(key) != KEY_BYTES):
            raise ValueError("invalid analysis payload key")
        self._key = key

    def _read_key(self):
        try:
            checked_path(self.key_path)
            if not self.key_path.is_file() or self.key_path.stat().st_size > 16384:
                raise ValueError("invalid key file")
            key = self._unprotect(base64.b64decode(self.key_path.read_bytes(), validate=True))
            if not isinstance(key, bytes) or len(key) != KEY_BYTES:
                raise ValueError("invalid key length")
            return key
        except Exception as exc:
            raise PayloadUnreadable() from exc

    def _load_key(self, *, create=False):
        with self.lock:
            if self.key_path is None:
                if self._key is not None:
                    return self._key
                if not create:
                    raise PayloadUnreadable()
                self._key = os.urandom(KEY_BYTES)
                return self._key
            path = checked_path(self.key_path)
            with ProcessLock(path.with_name(path.name + ".lock")).acquire(timeout=10):
                marker = path.with_name("payload-format.json")
                if path.exists():
                    key = self._read_key()
                    if self._key is not None and key != self._key:
                        raise PayloadUnreadable()
                else:
                    if not create or marker.exists() or self._key is not None:
                        raise PayloadUnreadable()
                    key = os.urandom(KEY_BYTES)
                    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        blob = base64.b64encode(self._protect(key))
                        with temporary.open("xb") as stream:
                            stream.write(blob)
                            stream.flush()
                            os.fsync(stream.fileno())
                        # Publish a complete file without replacing a winner's key.
                        try:
                            os.link(temporary, path)
                        except FileExistsError:
                            key = self._read_key()
                    except PayloadUnreadable:
                        raise
                    except Exception as exc:
                        raise PayloadUnreadable("无法安全保存分析密钥，本次分析未写入") from exc
                    finally:
                        temporary.unlink(missing_ok=True)
                # An old updater must not carry encrypted rows into an older reader.
                # Keep a format marker even if somebody later removes the key file.
                self._ensure_marker(marker)
                self._key = key
                return key

    @staticmethod
    def _ensure_marker(marker):
        expected = {"schema": 1, "payloadFormat": ENVELOPE}
        temporary = marker.with_name(marker.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            checked_path(marker)
            if not marker.exists():
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(expected, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, marker)
                except FileExistsError:
                    pass
            if marker.stat().st_size > 4096 or json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise ValueError("invalid payload format marker")
        except (OSError, ValueError, TypeError) as exc:
            raise PayloadUnreadable("分析格式标记不可用，已有文件已保留") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def protect(self, text):
        if not isinstance(text, str):
            raise TypeError("payload must be a string")
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._load_key(create=True)).encrypt(nonce, text.encode("utf-8"), None)
        return ENVELOPE + base64.b64encode(nonce + sealed).decode("ascii")

    def unprotect(self, stored):
        if not isinstance(stored, str):
            raise TypeError("payload must be a string")
        if not stored.startswith(ENVELOPE):
            return stored
        try:
            blob = base64.b64decode(stored[len(ENVELOPE):], validate=True)
            if len(blob) < NONCE_BYTES + 16:
                raise ValueError("short encrypted payload")
            return AESGCM(self._load_key()).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None).decode("utf-8")
        except (binascii.Error, ValueError, InvalidTag, UnicodeError) as exc:
            raise PayloadUnreadable() from exc

    def dumps(self, value):
        return self.protect(json.dumps(value, ensure_ascii=False))

    def backup_key(self):
        """Retain one protected key backup; never replace an existing backup."""
        if self.key_path is None:
            return None
        self._load_key(create=True)
        path = checked_path(self.key_path)
        backup = path.with_name("analysis-key.backup.bin")
        with ProcessLock(path.with_name(path.name + ".lock")).acquire(timeout=10):
            checked_path(backup)
            if backup.exists():
                if backup.read_bytes() != path.read_bytes():
                    raise PayloadUnreadable("分析密钥备份与当前密钥不一致，已有文件已保留")
                return backup
            temporary = backup.with_name(backup.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(path.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, backup)
            finally:
                temporary.unlink(missing_ok=True)
        return backup

    def loads(self, stored):
        if stored is None or stored == "":
            return None
        try:
            return json.loads(self.unprotect(stored))
        except (ValueError, TypeError) as exc:
            raise PayloadUnreadable() from exc


_default_lock = threading.Lock()
_default_ciphers = {}


def default_cipher(root=None):
    if root is None:
        from backend_contracts import ROOT
        root = ROOT
    from product_profile import current_product
    path = current_product().state_dir("real-client-runtime", Path(root).resolve()) / KEY_FILENAME
    with _default_lock:
        return _default_ciphers.setdefault(str(path), PayloadCipher(path))

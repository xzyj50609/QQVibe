"""Validated API model settings and Windows user-scoped encrypted key storage.

The store holds a saved API profile; the analysis backend decides which source is
actually active. Reading a saved profile must never imply a successful switch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from backend_contracts import LOCAL_SOURCE_ID, ModelSourceUnavailable


PROTOCOLS = frozenset({"anthropic", "responses", "chat_completions", "gemini", "ollama"})
MAX_BASE_URL = 2048
MAX_MODEL_ID = 256
MAX_API_KEY = 4096
MIN_CONTEXT_TOKENS = 4096
MAX_CONTEXT_TOKENS = 1000000
_MISSING = object()


def _source_fingerprint(protocol, base_url, model):
    """Keep source identity stable across key changes and context budgets."""
    identity = json.dumps(["api-source-v1", protocol, base_url, model],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _text(value, name, maximum):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid " + name)
    return value


def connection_values(request, require_model=False):
    """Validate user-controlled endpoint fields before handing them to an SDK."""
    if not isinstance(request, dict):
        raise ValueError("invalid model source request")
    allowed = {"protocol", "baseUrl", "apiKey", "model", "contextTokens"} if require_model else {"protocol", "baseUrl", "apiKey"}
    required = {"protocol", "baseUrl", "model"} if require_model else {"protocol", "baseUrl"}
    if not required <= request.keys() or not request.keys() <= allowed:
        raise ValueError("invalid model source request")
    protocol = request["protocol"]
    if not isinstance(protocol, str) or protocol not in PROTOCOLS:
        raise ValueError("invalid protocol")
    base_url = _text(request["baseUrl"], "baseUrl", MAX_BASE_URL).strip()
    if not base_url or any(char in base_url for char in ("\\", " ", "\t", "\r", "\n")):
        raise ValueError("invalid baseUrl")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port  # Also rejects malformed numeric ports.
    except ValueError as exc:
        raise ValueError("invalid baseUrl") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or port == 0 or
            any(part in (".", "..") for part in parsed.path.split("/"))):
        raise ValueError("invalid baseUrl")
    if parsed.hostname.endswith("."):
        raise ValueError("invalid baseUrl")
    base_url = base_url.rstrip("/")
    supplied_key = request.get("apiKey")
    if supplied_key is not None:
        if not isinstance(supplied_key, str) or len(supplied_key) > MAX_API_KEY or any(
                ord(char) < 32 or ord(char) == 127 for char in supplied_key):
            raise ValueError("invalid apiKey")
        supplied_key = supplied_key or None
    result = {"protocol": protocol, "baseUrl": base_url, "apiKey": supplied_key}
    if require_model:
        result["model"] = _text(request["model"], "model", MAX_MODEL_ID).strip()
        if not result["model"]:
            raise ValueError("invalid model")
        context_tokens = request.get("contextTokens")
        if context_tokens is not None and (type(context_tokens) is not int or
                not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
            raise ValueError("invalid contextTokens")
        result["contextTokens"] = context_tokens
    return result


def _dpapi_protect(value):
    import win32crypt

    return win32crypt.CryptProtectData(value.encode("utf-8"), "WechatVibe API key", None,
                                       None, None, 0x1)


def _dpapi_unprotect(value):
    import win32crypt

    return win32crypt.CryptUnprotectData(value, None, None, None, 0x1)[1].decode("utf-8")


class ModelSourceStore:
    def __init__(self, path, *, root=None, legacy_path=None, protect=None, unprotect=None):
        self.path = Path(path)
        self.root = Path(root) if root is not None else self.path.parent.parent.parent
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self.protect = protect or _dpapi_protect
        self.unprotect = unprotect or _dpapi_unprotect
        self.lock = threading.RLock()

    def _check_path(self, path):
        root = self.root.resolve()
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(root):
            raise ModelSourceUnavailable("model settings unavailable")
        cursor = path
        while cursor != self.root and cursor.is_relative_to(self.root):
            try:
                stat = cursor.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ModelSourceUnavailable("model settings unavailable") from exc
            else:
                if cursor.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    raise ModelSourceUnavailable("model settings unavailable")
            cursor = cursor.parent

    def _read_json(self, path):
        self._check_path(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _MISSING
        except (OSError, ValueError, UnicodeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc

    def _read(self):
        raw = self._read_json(self.path)
        if raw is _MISSING and self.legacy_path is not None:
            raw = self._read_json(self.legacy_path)
            # The old shared filename also held local model-directory settings.
            if isinstance(raw, dict) and raw.get("schema") == 1 and "version" not in raw:
                return None
        if raw is _MISSING:
            return None
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("api"), dict):
            raise ModelSourceUnavailable("model settings unavailable")
        api = raw["api"]
        try:
            validated = connection_values({"protocol": api["protocol"], "baseUrl": api["baseUrl"],
                                           "model": api["model"],
                                           "contextTokens": api.get("contextTokens")}, require_model=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        encrypted = api.get("encryptedKey")
        if encrypted is not None and (not isinstance(encrypted, str) or len(encrypted) > 16384):
            raise ModelSourceUnavailable("model settings unavailable")
        source_id = raw.get("sourceId")
        if not isinstance(source_id, str) or len(source_id) != 32 or any(
                char not in "0123456789abcdef" for char in source_id):
            raise ModelSourceUnavailable("model settings unavailable")
        selected_mode = raw.get("selectedMode", "api")
        if selected_mode not in ("local", "api"):
            raise ModelSourceUnavailable("model settings unavailable")
        source_ids = raw.get("sourceIds", _MISSING)
        if source_ids is not _MISSING and (not isinstance(source_ids, dict) or any(
                not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                any(char not in "0123456789abcdef" for char in fingerprint) or
                not isinstance(value, str) or len(value) != 32 or
                any(char not in "0123456789abcdef" for char in value)
                for fingerprint, value in source_ids.items()) or
                len(set(source_ids.values())) != len(source_ids)):
            raise ModelSourceUnavailable("model settings unavailable")
        saved = {"api": {"protocol": validated["protocol"], "baseUrl": validated["baseUrl"],
                         "model": validated["model"], "contextTokens": validated["contextTokens"],
                         "encryptedKey": encrypted},
                 "sourceId": source_id, "selectedMode": selected_mode}
        if source_ids is not _MISSING:
            saved["sourceIds"] = source_ids
        return saved

    def _decrypted_key(self, encrypted):
        try:
            return self.unprotect(base64.b64decode(encrypted, validate=True))
        except Exception as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc

    def _source_ids(self, saved):
        source_ids = dict(saved.get("sourceIds", {})) if saved else {}
        if not saved or "sourceIds" in saved:
            return source_ids
        # Version 1 held only the last profile. Preserve that source's cache ID
        # before another API selection replaces it.
        api = saved["api"]
        fingerprint = _source_fingerprint(api["protocol"], api["baseUrl"], api["model"])
        source_ids[fingerprint] = saved["sourceId"]
        return source_ids

    def saved_selection(self):
        """Intent for startup restore; never proof that the worker switched."""
        with self.lock:
            saved = self._read()
        return saved or {"selectedMode": "local", "api": None, "sourceId": LOCAL_SOURCE_ID}

    def public(self, mode="local", source_id=LOCAL_SOURCE_ID, status="active"):
        with self.lock:
            saved = self._read()
        api = saved["api"] if saved else None
        return {"mode": mode,
                "api": ({"protocol": api["protocol"], "baseUrl": api["baseUrl"],
                         "model": api["model"], "contextTokens": api["contextTokens"],
                         "hasKey": bool(api["encryptedKey"])} if api else None),
                "sourceId": source_id, "status": status}

    def resolve_key(self, protocol, base_url, supplied_key):
        """A blank field can reuse a key only for its original protocol and host/path."""
        if supplied_key is not None:
            return supplied_key
        with self.lock:
            saved = self._read()
            if (not saved or saved["api"]["protocol"] != protocol or
                    saved["api"]["baseUrl"] != base_url or not saved["api"]["encryptedKey"]):
                return None
            return self._decrypted_key(saved["api"]["encryptedKey"])

    def save_api(self, protocol, base_url, model, key, source_id=None, context_tokens=None):
        """Call only after the analysis worker has atomically activated this source."""
        if context_tokens is not None and (type(context_tokens) is not int or
                not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
            raise ValueError("invalid contextTokens")
        with self.lock:
            saved = self._read()
            source_ids = self._source_ids(saved)
            fingerprint = _source_fingerprint(protocol, base_url, model)
            encrypted = None
            if key:
                try:
                    encrypted = base64.b64encode(self.protect(key)).decode("ascii")
                except Exception as exc:
                    raise ModelSourceUnavailable("model settings unavailable") from exc
            elif saved and (saved["api"]["protocol"], saved["api"]["baseUrl"]) == (protocol, base_url):
                encrypted = saved["api"]["encryptedKey"]
            source_id = source_ids.get(fingerprint, source_id or uuid.uuid4().hex)
            if not isinstance(source_id, str) or len(source_id) != 32 or any(
                    char not in "0123456789abcdef" for char in source_id):
                raise ValueError("invalid sourceId")
            if source_id in source_ids.values() and source_ids.get(fingerprint) != source_id:
                raise ValueError("duplicate sourceId")
            source_ids[fingerprint] = source_id
            self._write({"version": 1, "sourceId": source_id, "selectedMode": "api",
                        "api": {"protocol": protocol, "baseUrl": base_url, "model": model,
                                 "contextTokens": context_tokens,
                                 "encryptedKey": encrypted}, "sourceIds": source_ids})
            return source_id

    def save_local(self):
        """Remember local selection while retaining the encrypted API profile."""
        with self.lock:
            saved = self._read()
            if saved:
                self._write({"version": 1, **saved, "selectedMode": "local",
                             "sourceIds": self._source_ids(saved)})

    def clear_key(self):
        with self.lock:
            saved = self._read()
            if saved and (saved["api"]["encryptedKey"] or saved["selectedMode"] != "local"):
                source_ids = self._source_ids(saved)
                saved["api"]["encryptedKey"] = None
                self._write({"version": 1, **saved, "selectedMode": "local",
                             "sourceIds": source_ids})

    def _write(self, data):
        self._check_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_path(self.path)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)

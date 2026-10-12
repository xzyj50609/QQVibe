"""Validated API model profiles and Windows user-scoped encrypted key storage.

The store holds any number of named API profiles plus the analysis backend's
choice of active profile. Reading a saved profile must never imply a successful
switch, and saving a profile must never imply that it became active.
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
MAX_PROFILE_NAME = 64
MAX_PROFILES = 20
MIN_CONTEXT_TOKENS = 4096
MAX_CONTEXT_TOKENS = 1000000
LOCAL_SOURCE_LABEL = "本地 Laya"
SCHEMA_VERSION = 2
_MISSING = object()
_HEX = frozenset("0123456789abcdef")


def _source_fingerprint(protocol, base_url, model):
    """Keep source identity stable across key changes and context budgets."""
    identity = json.dumps(["api-source-v1", protocol, base_url, model],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _is_hex32(value):
    return isinstance(value, str) and len(value) == 32 and not set(value) - _HEX


def _text(value, name, maximum):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid " + name)
    return value


def profile_name(value, fallback):
    """A blank or missing name falls back to the model ID so every profile is labelable."""
    if value is None or value == "":
        return fallback
    return _text(value, "name", MAX_PROFILE_NAME).strip() or fallback


def connection_values(request, require_model=False):
    """Validate user-controlled endpoint fields before handing them to an SDK."""
    if not isinstance(request, dict):
        raise ValueError("invalid model source request")
    allowed = {"protocol", "baseUrl", "apiKey", "model", "contextTokens", "profileId"} if require_model else {"protocol", "baseUrl", "apiKey", "profileId"}
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
    if "profileId" in request:
        if not _is_hex32(request["profileId"]):
            raise ValueError("invalid profileId")
        result["profileId"] = request["profileId"]
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

    def _decode_profile(self, entry):
        if not isinstance(entry, dict) or not _is_hex32(entry.get("id")):
            raise ModelSourceUnavailable("model settings unavailable")
        try:
            validated = connection_values({"protocol": entry["protocol"], "baseUrl": entry["baseUrl"],
                                           "model": entry["model"],
                                           "contextTokens": entry.get("contextTokens")}, require_model=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        encrypted = entry.get("encryptedKey")
        if encrypted is not None and (not isinstance(encrypted, str) or len(encrypted) > 16384):
            raise ModelSourceUnavailable("model settings unavailable")
        try:
            name = profile_name(entry.get("name"), validated["model"])
        except ValueError as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        return {"id": entry["id"], "name": name, "protocol": validated["protocol"],
                "baseUrl": validated["baseUrl"], "model": validated["model"],
                "contextTokens": validated["contextTokens"], "encryptedKey": encrypted}

    def _decode_profiles(self, raw, version):
        """Version 1 held a single profile; it becomes the first entry of the list."""
        if version == 1:
            api = raw.get("api")
            if not isinstance(api, dict):
                raise ModelSourceUnavailable("model settings unavailable")
            return [self._decode_profile({**api, "id": raw.get("sourceId"),
                                          "name": api.get("name") or api.get("model")})]
        profiles = raw.get("profiles")
        if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
            raise ModelSourceUnavailable("model settings unavailable")
        decoded = [self._decode_profile(entry) for entry in profiles]
        if len({item["id"] for item in decoded}) != len(decoded):
            raise ModelSourceUnavailable("model settings unavailable")
        return decoded

    def _read(self):
        raw = self._read_json(self.path)
        if raw is _MISSING and self.legacy_path is not None:
            raw = self._read_json(self.legacy_path)
            # The old shared filename also held local model-directory settings.
            if isinstance(raw, dict) and raw.get("schema") == 1 and "version" not in raw:
                return None
        if raw is _MISSING:
            return None
        if not isinstance(raw, dict) or raw.get("version") not in (1, SCHEMA_VERSION):
            raise ModelSourceUnavailable("model settings unavailable")
        source_id = raw.get("sourceId")
        if not _is_hex32(source_id):
            raise ModelSourceUnavailable("model settings unavailable")
        selected_mode = raw.get("selectedMode", "api")
        if selected_mode not in ("local", "api"):
            raise ModelSourceUnavailable("model settings unavailable")
        # Version 1's fingerprint map belonged to the single-profile model: it appended an
        # entry per model change, so two fingerprints pointing at one id was the normal state
        # of a file many upgrades still carry. That map is discarded below and rebuilt from
        # the profile, so holding it to the current schema's uniqueness rule would make the
        # whole configuration unreadable for no reason. Only a current-schema file is judged
        # by it, where duplicates really would mean a profile owns two identities.
        source_ids = raw.get("sourceIds", _MISSING)
        if raw["version"] == SCHEMA_VERSION:
            if source_ids is not _MISSING and (not isinstance(source_ids, dict) or any(
                    not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                    any(char not in _HEX for char in fingerprint) or
                    not _is_hex32(value)
                    for fingerprint, value in source_ids.items()) or
                    len(set(source_ids.values())) != len(source_ids)):
                raise ModelSourceUnavailable("model settings unavailable")
        else:
            source_ids = _MISSING
        profiles = self._decode_profiles(raw, raw["version"])
        if raw["version"] == 1:
            # Preserve valid historical cache identities. Legacy duplicate identities
            # are ambiguous: the active endpoint owns its ID, other collisions are
            # ignored rather than making a readable configuration unusable.
            active = profiles[0]
            current = _source_fingerprint(active["protocol"], active["baseUrl"], active["model"])
            source_ids = {current: source_id}
            legacy_ids = raw.get("sourceIds")
            if isinstance(legacy_ids, dict):
                for fingerprint, value in legacy_ids.items():
                    if (isinstance(fingerprint, str) and len(fingerprint) == 64 and
                            not set(fingerprint) - _HEX and _is_hex32(value) and
                            fingerprint not in source_ids and value not in source_ids.values()):
                        source_ids[fingerprint] = value
        saved = {"profiles": profiles, "sourceId": source_id, "selectedMode": selected_mode}
        if source_ids is not _MISSING:
            saved["sourceIds"] = source_ids
        saved["api"] = next((item for item in profiles if item["id"] == source_id), None)
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
        if api:
            fingerprint = _source_fingerprint(api["protocol"], api["baseUrl"], api["model"])
            source_ids[fingerprint] = saved["sourceId"]
        return source_ids

    def saved_selection(self):
        """Intent for startup restore; never proof that the worker switched."""
        with self.lock:
            saved = self._read()
            if saved:
                api = saved["api"]
                return {**saved, "profileId": saved["sourceId"],
                        "sourceId": self._source_ids(saved).get(_source_fingerprint(
                            api["protocol"], api["baseUrl"], api["model"]), api["id"])
                        if api else LOCAL_SOURCE_ID}
        return {"selectedMode": "local", "api": None, "profiles": [],
                "profileId": None, "sourceId": LOCAL_SOURCE_ID}

    def profile_source_id(self, profile_id):
        """Inference cache identity, independent of the editable profile's stable ID."""
        with self.lock:
            saved = self._read()
            profile = next((item for item in (saved["profiles"] if saved else [])
                            if item["id"] == profile_id), None)
            if profile is None:
                raise ValueError("invalid profileId")
            fingerprint = _source_fingerprint(profile["protocol"], profile["baseUrl"], profile["model"])
            return self._source_ids(saved).get(fingerprint, profile["id"])

    @staticmethod
    def _public_profile(profile):
        return {"id": profile["id"], "name": profile["name"], "label": profile["name"],
                "protocol": profile["protocol"], "baseUrl": profile["baseUrl"],
                "model": profile["model"], "contextTokens": profile["contextTokens"],
                "hasKey": bool(profile["encryptedKey"])}

    def public(self, mode="local", source_id=LOCAL_SOURCE_ID, status="active"):
        with self.lock:
            saved = self._read()
        profiles = saved["profiles"] if saved else []
        active = saved["api"] if saved else None
        return {"mode": mode,
                "api": ({"id": active["id"], "protocol": active["protocol"],
                         "baseUrl": active["baseUrl"], "model": active["model"],
                         "contextTokens": active["contextTokens"],
                         "hasKey": bool(active["encryptedKey"])} if active else None),
                "profiles": [self._public_profile(item) for item in profiles],
                "label": active["name"] if mode == "api" and active else LOCAL_SOURCE_LABEL,
                "sourceId": source_id, "status": status}

    def profile(self, profile_id):
        """One stored profile including its encrypted key, or None."""
        if not _is_hex32(profile_id):
            return None
        with self.lock:
            saved = self._read()
        if not saved:
            return None
        found = next((item for item in saved["profiles"] if item["id"] == profile_id), None)
        return dict(found) if found else None

    def profile_for_endpoint(self, protocol, base_url, model):
        """The stored profile that covers this exact connection, if any."""
        with self.lock:
            saved = self._read()
            if not saved:
                return None
            candidates = ([saved["api"]] if saved["api"] else []) + saved["profiles"]
            found = next((item for item in candidates if
                          (item["protocol"], item["baseUrl"], item["model"]) ==
                          (protocol, base_url, model)), None)
            return dict(found) if found else None

    def resolve_key(self, protocol, base_url, supplied_key, *, profile_id=_MISSING):
        """Explicit profile edits only reuse that profile's own key at the same endpoint.

        Passing None denotes a new profile. Omitted profile_id retains the legacy
        connection-only API; it is never used by profile creation/editing routes.
        """
        if supplied_key is not None:
            return supplied_key
        with self.lock:
            saved = self._read()
            if not saved:
                return None
            if profile_id is not _MISSING:
                if profile_id is None:
                    return None
                target = next((item for item in saved["profiles"] if item["id"] == profile_id), None)
                if target is None:
                    raise ValueError("invalid profileId")
                if (target["protocol"], target["baseUrl"]) != (protocol, base_url):
                    return None
                return self._decrypted_key(target["encryptedKey"]) if target["encryptedKey"] else None
            active = saved["api"]
            ordered = ([active] if active else []) + [item for item in saved["profiles"]
                                                       if item is not active]
            for profile in ordered:
                if (profile["protocol"] == protocol and profile["baseUrl"] == base_url and
                        profile["encryptedKey"]):
                    return self._decrypted_key(profile["encryptedKey"])
            return None

    def _encrypt(self, key):
        try:
            return base64.b64encode(self.protect(key)).decode("ascii")
        except Exception as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc

    def _upsert_profile(self, saved, profile_id, name, protocol, base_url, model, key, context_tokens):
        """Return (profiles, profile_id, source_ids) for one create-or-update."""
        profiles = [dict(item) for item in (saved["profiles"] if saved else [])]
        source_ids = dict(self._source_ids(saved))
        index = None
        if profile_id is not None:
            if not _is_hex32(profile_id):
                raise ValueError("invalid profileId")
            index = next((position for position, item in enumerate(profiles)
                          if item["id"] == profile_id), None)
            if index is None:
                raise ValueError("invalid profileId")
        if key:
            encrypted = self._encrypt(key)
        elif index is not None and profiles[index]["protocol"] == protocol and \
                profiles[index]["baseUrl"] == base_url:
            # A blank key field may only keep the key this profile already stored
            # for this exact protocol and host/path.
            encrypted = profiles[index]["encryptedKey"]
        else:
            encrypted = None
        fields = {"name": name, "protocol": protocol, "baseUrl": base_url, "model": model,
                  "contextTokens": context_tokens, "encryptedKey": encrypted}
        if index is None:
            if len(profiles) >= MAX_PROFILES:
                raise ValueError("too many model profiles")
            profile_id = uuid.uuid4().hex
            profiles.append({"id": profile_id, **fields})
        else:
            profiles[index].update(fields)
            profile_id = profiles[index]["id"]
        # Cache identity belongs to the actual inference endpoint/model, not the
        # profile row. Retain old identities so m1 -> m2 -> m1 restores m1's cache.
        fingerprint = _source_fingerprint(protocol, base_url, model)
        if fingerprint not in source_ids:
            source_ids[fingerprint] = (profile_id if profile_id not in source_ids.values()
                                       else uuid.uuid4().hex)
        return profiles, profile_id, source_ids

    def _write_document(self, profiles, source_id, selected_mode, source_ids):
        self._write({"version": SCHEMA_VERSION, "sourceId": source_id,
                     "selectedMode": selected_mode, "profiles": profiles,
                     "sourceIds": source_ids})

    def save_api(self, protocol, base_url, model, key, source_id=None, context_tokens=None):
        """Call only after the analysis worker has atomically activated this source."""
        if context_tokens is not None and (type(context_tokens) is not int or
                not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
            raise ValueError("invalid contextTokens")
        with self.lock:
            saved = self._read()
            source_ids = self._source_ids(saved)
            fingerprint = _source_fingerprint(protocol, base_url, model)
            existing = self.profile_for_endpoint(protocol, base_url, model)
            profile_id = existing["id"] if existing else None
            mapped = source_ids.get(fingerprint)
            if source_id is not None and (not _is_hex32(source_id) or
                    source_id in source_ids.values() and mapped != source_id):
                raise ValueError("invalid sourceId")
            if profile_id is not None:
                if not _is_hex32(profile_id):
                    raise ValueError("invalid sourceId")
            profiles, profile_id, source_ids = self._upsert_profile(
                saved, profile_id, profile_name(None, model), protocol, base_url, model,
                key, context_tokens)
            if source_id is not None:
                if mapped is not None and mapped != source_id:
                    raise ValueError("invalid sourceId")
                source_ids[fingerprint] = source_id
            self._write_document(profiles, profile_id, "api", source_ids)
            return source_ids[fingerprint]

    def save_profile(self, profile_id, name, protocol, base_url, model, key, context_tokens):
        """Create or update one profile. The active selection never changes here."""
        if context_tokens is not None and (type(context_tokens) is not int or
                not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
            raise ValueError("invalid contextTokens")
        with self.lock:
            saved = self._read()
            profiles, profile_id, source_ids = self._upsert_profile(
                saved, profile_id, profile_name(name, model), protocol, base_url, model,
                key, context_tokens)
            self._write_document(profiles, saved["sourceId"] if saved else uuid.uuid4().hex,
                                 saved["selectedMode"] if saved else "local", source_ids)
            return profile_id

    def select_profile(self, profile_id):
        """Point the saved selection at an existing profile without probing it."""
        with self.lock:
            saved = self._read()
            if not saved or not any(item["id"] == profile_id for item in saved["profiles"]):
                raise ValueError("invalid profileId")
            self._write_document(saved["profiles"], profile_id, "api", self._source_ids(saved))

    def delete_profile(self, profile_id):
        """Remove one profile; report whether the active selection was removed too."""
        with self.lock:
            saved = self._read()
            profiles = [item for item in (saved["profiles"] if saved else [])
                        if item["id"] != profile_id]
            if not saved or len(profiles) == len(saved["profiles"]):
                raise ValueError("invalid profileId")
            was_active = saved["sourceId"] == profile_id
            source_ids = self._source_ids(saved)
            self._write_document(profiles, saved["sourceId"],
                                 "local" if was_active else saved["selectedMode"], source_ids)
            return was_active

    def save_local(self):
        """Remember local selection while retaining every encrypted API profile."""
        with self.lock:
            saved = self._read()
            if saved:
                self._write_document(saved["profiles"], saved["sourceId"], "local",
                                     self._source_ids(saved))

    def clear_key(self, profile_id=None):
        """Drop one profile's key. Reports whether the caller must fall back to local."""
        with self.lock:
            saved = self._read()
            if not saved:
                return False
            profiles = [dict(item) for item in saved["profiles"]]
            target = saved["sourceId"] if profile_id is None else profile_id
            index = next((position for position, item in enumerate(profiles)
                          if item["id"] == target), None)
            if index is None:
                if profile_id is None:
                    return False
                raise ValueError("invalid profileId")
            was_active = saved["sourceId"] == target and saved["selectedMode"] == "api"
            profiles[index]["encryptedKey"] = None
            self._write_document(profiles, saved["sourceId"],
                                 "local" if was_active else saved["selectedMode"],
                                 self._source_ids(saved))
            return was_active

    def _write(self, data):
        self._check_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_path(self.path)
        previous = self._read_json(self.path)
        if isinstance(previous, dict) and previous.get("version") == 1:
            backup = self.path.with_name(self.path.name + ".before-profiles-v2.bak")
            self._check_path(backup)
            pending_backup = backup.with_name(backup.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with pending_backup.open("x", encoding="utf-8") as stream:
                    json.dump(previous, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                # Publish only a complete durable backup, without replacing one
                # produced by an earlier migration or another process.
                os.link(pending_backup, backup)
            except FileExistsError:
                if self._read_json(backup) != previous:
                    raise ModelSourceUnavailable("model settings migration backup differs") from None
            except OSError as exc:
                raise ModelSourceUnavailable("model settings migration backup unavailable") from exc
            finally:
                pending_backup.unlink(missing_ok=True)
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

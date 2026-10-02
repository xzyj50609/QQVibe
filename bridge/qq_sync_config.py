"""QQ-only connection configuration, with Windows user-scoped encrypted bearer."""
from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from pathlib import Path

from account_store import _check_root, _regular
from model_source import _dpapi_protect, _dpapi_unprotect
from qq_connector import ConnectorError
from qq_identity import canonical_uin
from qce_protocol import validate_base, ProbeFailure


def validate_connection_input(base, owner, token=None):
    """Validate syntax before the coordinator cancels a working connection."""
    try:
        base, owner = validate_base(base), canonical_uin(owner)
    except (ValueError, ProbeFailure):
        raise ConnectorError("configuration-invalid") from None
    if token is not None and (not isinstance(token, str) or not token or len(token) > 4096 or
            any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise ConnectorError("configuration-invalid")
    return base, owner


class SyncConnectionStore:
    def __init__(self, path, *, protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        self.path = Path(os.path.abspath(path))
        self.lock = threading.RLock()
        self.protect, self.unprotect = protect, unprotect

    def read(self):
        with self.lock:
            _check_root(self.path.parent)
            if not _regular(self.path):
                return {"version": 1, "enabled": False, "baseUrl": None, "ownerUin": None, "bearer": None}
            try:
                if self.path.stat().st_size > 16384:
                    raise ValueError()
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if set(value) not in ({"version", "enabled", "baseUrl", "ownerUin", "bearer"},
                        {"version", "enabled", "baseUrl", "ownerUin", "bearer",'acceptedUnverifiedVersion'}) or type(value["version"]) is not int or value["version"] != 1 or type(value["enabled"]) is not bool:
                    raise ValueError()
                if 'acceptedUnverifiedVersion' in value:
                    from qq_compatibility import known_version
                    if known_version(value['acceptedUnverifiedVersion'])=='unknown':raise ValueError()
                value["baseUrl"] = validate_base(value["baseUrl"])
                value["ownerUin"] = canonical_uin(value["ownerUin"])
                if value["bearer"] is not None and (not isinstance(value["bearer"], str) or len(value["bearer"]) > 8192):
                    raise ValueError()
                return value
            except (ValueError, TypeError, KeyError, OSError, ProbeFailure):
                raise ConnectorError("configuration-unavailable") from None

    def _write(self, value):
        _check_root(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _regular(self.path)
        temporary = self.path.with_name(self.path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
            _check_root(self.path.parent)
            _regular(self.path)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def configure(self, base, owner, token=None):
        base, owner = validate_connection_input(base, owner, token)
        with self.lock:
            saved = self.read()
            bearer = None
            if token is None:
                if saved["baseUrl"] == base and saved["ownerUin"] == owner:
                    bearer = saved["bearer"]
            else:
                try:
                    bearer = base64.b64encode(self.protect(token)).decode("ascii")
                except Exception:
                    raise ConnectorError("credential-storage-unavailable") from None
            self._write({"version": 1, "enabled": False, "baseUrl": base, "ownerUin": owner, "bearer": bearer})
            return self.public()

    def token(self):
        saved = self.read()
        if not saved["bearer"]:
            raise ConnectorError("auth-required")
        try:
            return self.unprotect(base64.b64decode(saved["bearer"], validate=True))
        except Exception:
            raise ConnectorError("credential-storage-unavailable") from None

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid-enabled-choice")
        with self.lock:
            saved = self.read()
            if not saved["ownerUin"]:
                if enabled:
                    raise ConnectorError("configuration-unavailable")
                return
            saved["enabled"] = enabled
            self._write(saved)

    def clear_token(self):
        with self.lock:
            saved = self.read()
            if saved["ownerUin"]:
                saved.update(enabled=False, bearer=None)
                self._write(saved)

    def accept_version(self,version):
        from qq_compatibility import known_version
        if known_version(version)=='unknown':raise ValueError('invalid-version-consent')
        with self.lock:
            saved=self.read()
            saved['acceptedUnverifiedVersion']=version
            self._write(saved)

    def public(self):
        saved = self.read()
        return {"enabled": saved["enabled"], "baseUrl": saved["baseUrl"], "ownerUin": saved["ownerUin"],
                "tokenConfigured": bool(saved["bearer"])}

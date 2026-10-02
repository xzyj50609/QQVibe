"""Read the standard QCE credential only after an explicit connection action.

No logs, account databases, recursive search or credential values in errors.
Non-standard ports/installations use the existing manual connection form.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from qq_connector import ConnectorError, QQConnector, failure_code

STANDARD_BASE = "http://127.0.0.1:40653"


def standard_connection(*, local_app_data=None, connector_factory=QQConnector):
    location = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not location or not Path(location).is_absolute():
        raise ConnectorError("standard-installation-missing")
    root = Path(location)
    path = root / "QQChatExporter" / ".qce-config" / "security.json"
    try:
        for item in (path, *path.parents):
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise ConnectorError("standard-configuration-invalid")
        with path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        config = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(config, dict) or config.get("tokenExpired") is True:
            raise ValueError()
        token = config.get("accessToken")
        if not isinstance(token, str) or not token or len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError()
        connector = connector_factory(STANDARD_BASE, token)
    except FileNotFoundError:
        raise ConnectorError("standard-installation-missing") from None
    except ConnectorError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise ConnectorError("standard-configuration-invalid") from None
    try:
        identity = connector.identity(connector.client())
    except Exception as error:
        raise ConnectorError(failure_code(error)) from None
    return STANDARD_BASE, identity["ownerUin"], token

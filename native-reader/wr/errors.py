"""Protocol error codes for the native reader sidecar.

Codes are stable, machine-readable identifiers. They never contain paths, keys, or message
bodies. The Electron layer maps them onto the frozen NativeWechatState values.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

# Request framing / validation
BAD_JSON = "bad_json"
BAD_REQUEST = "bad_request"
PAYLOAD_TOO_LARGE = "payload_too_large"
UNKNOWN_METHOD = "unknown_method"
VERSION_MISMATCH = "version_mismatch"
INTERNAL = "internal"

# Reader / WeChat states
NOT_CONNECTED = "not_connected"
WECHAT_CLOSED = "wechat_closed"
NOT_LOGGED_IN = "not_logged_in"
SELECT_ACCOUNT = "select_account"
UNLOCKING = "unlocking"
PERMISSION_DENIED = "permission_denied"
UNSUPPORTED = "unsupported"
UNSUPPORTED_SCHEMA = "unsupported_schema"
KEY_UNAVAILABLE = "key_unavailable"
READ_ERROR = "read_error"
SNAPSHOT_ERROR = "snapshot_error"
NOT_FOUND = "not_found"
BUSY = "busy"
CANCELLED = "cancelled"

# Error code -> frozen NativeWechatState mapping (only for status-bearing failures).
STATE_FOR_CODE = {
    NOT_CONNECTED: "disconnected",
    WECHAT_CLOSED: "wechat_closed",
    NOT_LOGGED_IN: "not_logged_in",
    SELECT_ACCOUNT: "select_account",
    UNLOCKING: "unlocking",
    PERMISSION_DENIED: "permission_denied",
    UNSUPPORTED: "unsupported",
    UNSUPPORTED_SCHEMA: "unsupported",
    KEY_UNAVAILABLE: "error",
    READ_ERROR: "error",
    SNAPSHOT_ERROR: "error",
    INTERNAL: "error",
}


class ProtocolError(Exception):
    """Raised inside a method handler; converted to an ``ok:false`` response."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class BadRequest(ProtocolError):
    def __init__(self, message: str) -> None:
        super().__init__(BAD_REQUEST, message)

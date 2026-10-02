"""Sanitized stderr logging.

Only finite state codes / method names may be written. Paths, usernames, message bodies, and keys
must never reach the log. stdout is reserved exclusively for the JSONL protocol.
"""

from __future__ import annotations

import sys

_ALLOWED = set(
    "start stop ready busy closed not_logged_in select_account unlocking permission_denied "
    "unsupported unsupported_schema key_unavailable read_error snapshot_error internal "
    "cancelled bad_json bad_request payload_too_large unknown_method version_mismatch "
    "connect disconnect accounts contacts open_history read_history latest window hello status".split()
)


def _emit(stream, code: str) -> None:
    safe = code if code in _ALLOWED else "internal"
    stream.write(f"[native-reader] {safe}\n")
    stream.flush()


def state(code: str) -> None:
    """Write a whitelisted state code to stderr."""
    _emit(sys.stderr, code)

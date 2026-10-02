"""Account anchoring for the QQ product (data-schema section 5, local-core-v1).

The account key is derived from the canonical UIN and never from the UID, so a UID
appearing later cannot open a second library for the same person.
"""
from __future__ import annotations

import hashlib
import re

CANONICAL_PREFIX = "qq:uin:"
ACCOUNT_KEY_PATTERN = re.compile(r"a:[0-9a-f]{32}")


def canonical_uin(value):
    """ASCII decimal, non-zero, whitespace-trimmed, leading zeros dropped; the raw stays elsewhere."""
    if isinstance(value, bool) or value is None:
        raise ValueError("missing uin")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError("invalid uin")
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError("invalid uin")
    trimmed = text.lstrip("0")
    if not trimmed:
        raise ValueError("zero uin")
    return trimmed


def account_key(uin):
    canonical = CANONICAL_PREFIX + canonical_uin(uin)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "a:" + digest[:32]


def is_account_key(value):
    return isinstance(value, str) and ACCOUNT_KEY_PATTERN.fullmatch(value) is not None

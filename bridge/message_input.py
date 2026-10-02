"""Unified message-input record and legacy wire projection.

This module is a pure contract boundary for the future OCR extension. It does not
read WeChat, decode media, or call any model. It normalizes an already-read source
row into a typed input record and projects it back to the legacy node wire
(``id``/``side``/``text``/``time``) plus an optional ``inputMeta`` envelope that is
carried only over the local IPC. Meta never reaches the model prompt.

Facts it relies on:
- The WeChat source layer already emits ``time`` as integer milliseconds and already
  multiplies sub-1e10 values by 1000. ``0`` means unknown. This module never multiplies
  ``time`` again.
- Only the current reply body is available from ``appmsg``/``title``; no quoted author,
  body or time is recovered here. A missing quote stays unknown, never guessed.

Legacy wire compatibility:
- ``id`` keeps the old "non-empty string" rule (no new length/control limits).
- ``time`` keeps a finite non-negative number (ints and floats alike); ``0`` is the
  unknown sentinel and is preserved as-is.
- New metadata fields are bounded by Unicode codepoint and type-checked. Names and
  quote text are display text, so newlines/tabs/emoji are allowed verbatim; only IDs
  reject control characters.
"""
from __future__ import annotations

import math

SOURCE_KINDS = frozenset({"wechat", "qq", "ocr", "unknown"})

MAX_ACCOUNT = 200
MAX_CONVERSATION = 256
MAX_SENDER_ID = 200
MAX_SENDER_NAME = 200
MAX_QUOTE_ID = 200
MAX_QUOTE_SENDER_ID = 200
MAX_QUOTE_SENDER_NAME = 200
MAX_QUOTE_TEXT = 4000


def _identifier(value, maximum, field):
    """ID-like new metadata: non-empty, bounded, control-free."""
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid " + field)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid " + field)
    return value


def _display_text(value, maximum, field):
    """Display text: bounded by codepoint but newlines/tabs/emoji stay verbatim."""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("invalid " + field)
    return value


def _optional_identifier(value, maximum, field):
    if value is None or value == "":
        return None
    return _identifier(value, maximum, field)


def _optional_display(value, maximum, field):
    if value is None or value == "":
        return None
    return _display_text(value, maximum, field)


def _sent_at_ms(value, field="sentAtMs"):
    """Already-millisecond number; 0 means unknown. Never multiplied again."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid " + field)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise ValueError("invalid " + field)
    return value


def normalize_quote(quote):
    """Validate an optional quote envelope without recovering missing content.

    Accepts the canonical ``{id,senderId,senderName,text,sentAtMs}`` shape and the
    draft aliases ``author`` -> ``senderName`` and ``time`` -> ``sentAtMs``. It never
    derives ``senderId`` from a display name.
    """
    if quote is None:
        return None
    if not isinstance(quote, dict):
        raise ValueError("invalid quote")
    sender_name = quote.get("senderName")
    if sender_name is None:
        sender_name = quote.get("author")
    sent_at = quote.get("sentAtMs")
    if sent_at is None:
        sent_at = quote.get("time")
    result = {
        "id": _optional_identifier(quote.get("id"), MAX_QUOTE_ID, "quote.id"),
        "senderId": _optional_identifier(quote.get("senderId"), MAX_QUOTE_SENDER_ID,
                                         "quote.senderId"),
        "senderName": _optional_display(sender_name, MAX_QUOTE_SENDER_NAME, "quote.senderName"),
        "text": _optional_display(quote.get("text"), MAX_QUOTE_TEXT, "quote.text"),
        "sentAtMs": _sent_at_ms(sent_at, "quote.sentAtMs"),
    }
    if all(value is None for value in result.values()):
        return None
    return result


def normalize_input_meta(value):
    """Validate an existing metadata envelope into the canonical shape."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid inputMeta")
    kind = "unknown"
    source = value.get("source")
    if source is not None:
        if not isinstance(source, dict):
            raise ValueError("invalid inputMeta.source")
        kind = source.get("kind")
        if kind is None:
            kind = "unknown"
        if not isinstance(kind, str) or kind not in SOURCE_KINDS:
            raise ValueError("invalid inputMeta.source.kind")
    return {
        "accountId": _optional_identifier(value.get("accountId"), MAX_ACCOUNT,
                                          "inputMeta.accountId"),
        "conversationId": _optional_identifier(value.get("conversationId"), MAX_CONVERSATION,
                                               "inputMeta.conversationId"),
        "senderId": _optional_identifier(value.get("senderId"), MAX_SENDER_ID,
                                         "inputMeta.senderId"),
        "senderName": _optional_display(value.get("senderName"), MAX_SENDER_NAME,
                                        "inputMeta.senderName"),
        "sentAtMs": _sent_at_ms(value.get("sentAtMs"), "inputMeta.sentAtMs"),
        "source": {"kind": kind},
        "quote": normalize_quote(value.get("quote")),
    }


def _merge_scope(trusted, foreign, maximum, field):
    """Trusted scope wins; a conflicting foreign metadata scope is rejected."""
    if trusted is None:
        return foreign
    validated = _identifier(trusted, maximum, field)
    if foreign is not None and foreign != validated:
        raise ValueError("cross-" + field + " envelope")
    return validated


def _merge_known(left, right, field):
    if left is None:
        return right
    if right is not None and left != right:
        raise ValueError("conflicting " + field)
    return left


def _merge_time(left, right):
    if left in (None, 0):
        return right if right is not None else left
    if right in (None, 0):
        return left
    return _merge_known(left, right, "sentAtMs")


def _merge_quote(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return {key: (_merge_time(left[key], right[key]) if key == "sentAtMs" else
                  _merge_known(left[key], right[key], "quote." + key)) for key in left}


def build_input_record(item, *, account_id=None, conversation_id=None, source_kind=None):
    """Normalize one source row into the unified input record.

    ``account_id``/``conversation_id`` are the caller's own trusted truth. An existing
    ``item['inputMeta']`` is validated and merged, never silently overwritten; a foreign
    scope is rejected instead of guessed. ``senderId``/``senderName`` come from the item
    (or its metadata) and stay null when missing, including SELF messages.
    """
    if not isinstance(item, dict):
        raise ValueError("invalid message item")
    stable_id = item.get("id")
    if not isinstance(stable_id, str) or not stable_id:
        raise ValueError("invalid id")
    side = item.get("side")
    if side not in ("self", "other"):
        raise ValueError("invalid side")
    text = item.get("text")
    if not isinstance(text, str):
        raise ValueError("invalid text")
    existing = normalize_input_meta(item.get("inputMeta"))
    account = _merge_scope(account_id, existing["accountId"] if existing else None,
                           MAX_ACCOUNT, "accountId")
    conversation = _merge_scope(conversation_id, existing["conversationId"] if existing else None,
                                MAX_CONVERSATION, "conversationId")
    sender_id = _merge_known(existing["senderId"] if existing else None,
                            _optional_identifier(item.get("senderId"), MAX_SENDER_ID, "senderId"), "senderId")
    sender_name = _merge_known(existing["senderName"] if existing else None,
                              _optional_display(item.get("senderName"), MAX_SENDER_NAME, "senderName"), "senderName")
    sent_at = _merge_time(existing["sentAtMs"] if existing else None,
                          _sent_at_ms(item.get("time"), "sentAtMs"))
    if source_kind is not None:
        if not isinstance(source_kind, str) or source_kind not in SOURCE_KINDS:
            raise ValueError("invalid source.kind")
        existing_kind = existing["source"]["kind"] if existing else "unknown"
        if existing_kind != "unknown" and source_kind != "unknown" and existing_kind != source_kind:
            raise ValueError("conflicting source.kind")
        kind = existing_kind if source_kind == "unknown" else source_kind
    elif existing:
        kind = existing["source"]["kind"]
    else:
        kind = "unknown"
    quote = _merge_quote(existing["quote"] if existing else None, normalize_quote(item.get("quote")))
    return {
        "id": stable_id,
        "side": side,
        "text": text,
        "accountId": account,
        "conversationId": conversation,
        "senderId": sender_id,
        "senderName": sender_name,
        "sentAtMs": sent_at,
        "source": {"kind": kind},
        "quote": quote,
    }


def record_to_meta(record):
    """Project a record to the local ``inputMeta`` envelope (no legacy fields)."""
    return {
        "accountId": record["accountId"],
        "conversationId": record["conversationId"],
        "senderId": record["senderId"],
        "senderName": record["senderName"],
        "sentAtMs": record["sentAtMs"],
        "source": {"kind": record["source"]["kind"]},
        "quote": record["quote"],
    }


def prepare_item(item, *, account_id=None, conversation_id=None, source_kind=None):
    """Return a shallow copy of ``item`` with a validated ``inputMeta`` attached.

    Legacy keys (``id``/``side``/``text``/``time``/...) are preserved byte-for-byte so
    the model wire stays identical whether or not metadata is carried.
    """
    record = build_input_record(item, account_id=account_id, conversation_id=conversation_id,
                                source_kind=source_kind)
    prepared = dict(item)
    prepared["inputMeta"] = record_to_meta(record)
    return prepared


def prepare_messages(messages, *, account_id=None, conversation_id=None, source_kind=None):
    return [prepare_item(item, account_id=account_id, conversation_id=conversation_id,
                         source_kind=source_kind) for item in messages]


def to_wire(item, record, *, target_cap=None, context_cap=None, is_target=False):
    """Project one record back to the legacy node wire plus optional ``inputMeta``.

    The legacy four fields keep their exact original values and truncation, so the
    model input is byte-identical whether or not metadata is carried.
    """
    text = record["text"]
    cap = target_cap if is_target else context_cap
    if cap is not None:
        text = text[:cap]
    wire = {"id": record["id"], "side": record["side"], "text": text}
    if "time" in item:
        wire["time"] = item["time"]
    wire["inputMeta"] = record_to_meta(record)
    return wire

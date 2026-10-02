"""Pure per-message API insight contract: label schema, revision, scope and shape.

This module must stay dependency-free (standard library only) and must not import
services, adapters, storage, the runtime, or the portrait contract module.

The API display contract is intentionally small: one short emotion and one short intent
per message. JSON/ID handling remains for transport, while semantic vocabulary is left to
the provider. The revision is separate so old, richer API rows are never reused by this
simple mode.
"""
from __future__ import annotations

FINE_LABEL_SCHEMA = "generic-v9"

API_INSIGHT_REVISION = "free-label-v5-simple"

API_INSIGHT_STATUSES = frozenset({"ok", "routine", "uncertain", "insufficient"})
API_AFFECT_FIELDS = ("tone", "feeling", "interaction")
API_INTENT_LIMIT = 1
API_LABEL_MIN = 1
API_LABEL_MAX = 4


def api_insight_scope(source_id):
    return source_id + ":" + API_INSIGHT_REVISION


def _is_han(character):
    code = ord(character)
    return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or
            0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x2FA1F)


def valid_api_insight_label(value):
    """A displayed short label: 1-4 Han characters and nothing else."""
    return (isinstance(value, str) and API_LABEL_MIN <= len(value) <= API_LABEL_MAX and
            all(_is_han(character) for character in value))


def normalize_api_insight(item):
    """Validate one insight item and return the canonical stored shape.

    New shape: ``{id,status}`` plus, for ``ok``, at most one short affect label and one
    short intent. The older affect/intents shape remains readable so saved historical
    rows can be displayed, but this revision stores the simple pair.

    Legacy scalar ``{emotion,intent}`` ok items are accepted for compatibility and
    normalized into the new shape (``emotion`` -> ``affect.feeling``, ``intent`` ->
    ``intents[0]``); the application always stores the new shape. Raises ``ValueError``
    on any violation and never invents a label.
    """
    if not isinstance(item, dict):
        raise ValueError("invalid-insights")
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("invalid-insights")
    status = item.get("status")
    if status not in API_INSIGHT_STATUSES:
        raise ValueError("invalid-insights")
    if status != "ok":
        affect = item.get("affect")
        intents = item.get("intents")
        if affect is not None and (not isinstance(affect, dict) or affect):
            raise ValueError("invalid-insights")
        if intents is not None and (not isinstance(intents, list) or intents):
            raise ValueError("invalid-insights")
        return {"id": identifier, "status": status}
    if "affect" in item or "intents" in item:
        affect = item.get("affect")
        if affect is None:
            affect = {}
        if not isinstance(affect, dict) or any(key not in API_AFFECT_FIELDS for key in affect):
            raise ValueError("invalid-insights")
        clean_affect = {}
        for field in API_AFFECT_FIELDS:
            label = affect.get(field)
            if label is None:
                continue
            if not valid_api_insight_label(label):
                raise ValueError("invalid-insights")
            clean_affect[field] = label
        values = list(clean_affect.values())
        if len(set(values)) != len(values):
            raise ValueError("invalid-insights")
        intents = item.get("intents")
        if intents is None:
            intents = []
        if not isinstance(intents, list) or len(intents) > API_INTENT_LIMIT:
            raise ValueError("invalid-insights")
        clean_intents = []
        for label in intents:
            if not valid_api_insight_label(label):
                raise ValueError("invalid-insights")
            if label in clean_intents:
                raise ValueError("invalid-insights")
            clean_intents.append(label)
        if set(clean_intents) & set(clean_affect.values()):
            raise ValueError("invalid-insights")
        result = {"id": identifier, "status": "ok"}
        if clean_affect:
            result["affect"] = clean_affect
        result["intents"] = clean_intents
        return result
    # Legacy scalar ok item; normalized into the new shape, never invented.
    emotion = item.get("emotion")
    intent = item.get("intent")
    if not valid_api_insight_label(emotion) or not valid_api_insight_label(intent):
        raise ValueError("invalid-insights")
    return {"id": identifier, "status": "ok", "affect": {"feeling": emotion},
            "intents": [intent]}

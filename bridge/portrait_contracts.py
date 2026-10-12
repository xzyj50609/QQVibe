"""Pure API portrait contract: revision and cache scope.

This module must stay dependency-free (standard library only) and must not import
services, adapters, storage, the runtime, or the message contract module.
"""
from __future__ import annotations

import hashlib
import json

API_PORTRAIT_REVISION = "portrait-v2"

PORTRAIT_EVIDENCE_DIMENSIONS = frozenset({
    "summary", "communication", "emotionExpression", "interactionPreferences",
    "topics", "patterns", "boundaries", "uncertain",
    "mbti_EI", "mbti_SN", "mbti_TF", "mbti_JP",
})


def empty_portrait_evidence(subject_kind="person"):
    return {"version": 3, "items": [], "targetCount": 0, "batchCount": 0,
            "subjectKind": subject_kind}


def valid_mbti_basis(value):
    """Optional axis explanations, separate from the eleven-field portrait."""
    if not isinstance(value, dict) or set(value) != {"EI", "SN", "TF", "JP"}:
        return False
    for axis in value.values():
        if (not isinstance(axis, dict) or
                set(axis) != {"status", "kind", "reason", "evidenceCount"} or
                axis["status"] not in ("supported", "insufficient", "unverified") or
                axis["kind"] not in ("pattern", "self-report", "unspecified") or
                not isinstance(axis["reason"], str) or len(axis["reason"]) > 160 or
                any(ord(char) < 32 or ord(char) == 127 for char in axis["reason"]) or
                type(axis["evidenceCount"]) is not int or
                not 0 <= axis["evidenceCount"] <= 100000):
            return False
    return True


def valid_portrait_evidence(value):
    """The v3 axis-specific ledger; keep in sync with api-portrait-evidence.ts.

    The database scope stays stable so earlier portraits remain visible. Older
    ledgers are intentionally invalidated for an explicit observation rebuild.
    """
    if (not isinstance(value, dict) or set(value) != {
            "version", "items", "targetCount", "batchCount", "subjectKind"} or
            type(value["version"]) is not int or value["version"] != 3 or
            type(value["targetCount"]) is not int or not 0 <= value["targetCount"] <= 9007199254740991 or
            type(value["batchCount"]) is not int or not 0 <= value["batchCount"] <= 9007199254740991 or
            value["subjectKind"] not in ("person", "group")):
        return False
    items = value.get("items")
    if not isinstance(items, list) or len(items) > 10000:
        return False
    def text_valid(text, maximum, *, quote=False):
        return (isinstance(text, str) and bool(text.strip()) and len(text) <= maximum and
                all((ord(char) >= 32 and ord(char) != 127) or
                    quote and char in "\n\r\t" for char in text))
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "dimension", "text", "sources"}:
            return False
        item_id, dimension, text, sources = (item["id"], item["dimension"],
                                              item["text"], item["sources"])
        if (not text_valid(item_id, 200) or item_id in seen or
                not isinstance(dimension, str) or dimension not in PORTRAIT_EVIDENCE_DIMENSIONS or
                not text_valid(text, 160) or not isinstance(sources, list) or not 1 <= len(sources) <= 8):
            return False
        for source in sources:
            if (not isinstance(source, dict) or set(source) != {"messageId", "quote", "time", "speaker"} or
                    not text_valid(source["messageId"], 200) or not text_valid(source["quote"], 240, quote=True) or
                    not text_valid(source["speaker"], 200) or
                    source["time"] is not None and (type(source["time"]) is not int or
                                                     not 0 <= source["time"] <= 9007199254740991)):
                return False
        seen.add(item_id)
    return True


def api_portrait_scope(source_id):
    return source_id + ":" + API_PORTRAIT_REVISION


def portrait_synthesis_fingerprint(evidence):
    """Identify semantic inputs, excluding bookkeeping-only progress changes."""
    if not valid_portrait_evidence(evidence):
        return None
    payload = {"items": sorted(evidence["items"], key=lambda item: item["id"]),
               "mbtiEligible": evidence["targetCount"] >= 100,
               "subjectKind": evidence["subjectKind"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def valid_synthesis_fingerprint(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(char in "0123456789abcdef" for char in value))

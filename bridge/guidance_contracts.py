"""Pure guidance contract: revision, scope, shape and the self/others split.

The deep-semantic analysis and the scenario advice arrive from one provider turn, so
they are validated together: a stored result can never show a reading that contradicts
the advice rendered beside it. This module stays dependency-free (standard library
only) and never imports services, storage, the runtime or the message/portrait
contracts. It re-checks the model output that ``electron/api-guidance.ts`` already
normalised, so a malformed provider reply can never be persisted or displayed.
"""
from __future__ import annotations

import hashlib
import json

GUIDANCE_REVISION = "api-guidance-v5"
REPLY_OPTIONS_REVISION = "api-reply-options-v2"

GUIDANCE_SCENARIOS = frozenset({"auto", "general", "leader"})

GUIDANCE_STATUSES = frozenset({"ok", "uncertain", "insufficient"})

GUIDANCE_POLARITIES = frozenset({"positive", "neutral", "negative", "mixed"})

SUBTEXT_STATUS_FIELDS = {"id", "status"}
SUBTEXT_BODY_FIELDS = {"id", "status", "surface", "implied", "tactic", "sentiment"}
SENTIMENT_FIELDS = {"polarity", "label"}
ADVICE_FIELDS = {"forOthers", "forSelf"}
OTHERS_FIELDS = {"reading", "strategies", "replies"}
REPLY_FIELDS = {"tone", "text"}
SELF_FIELDS = {"summary", "strengths", "improvements"}
RESULT_FIELDS = {"version", "scenario", "analyzeSelf", "subtexts", "advice"}
RESULT_OPTIONAL_FIELDS = {"usage", "responseId"}

MAX_SUBTEXTS = 12
# One guidance turn reasons over a bounded window, not the whole history.
GUIDANCE_WINDOW_MESSAGES = 160
GUIDANCE_CONTEXT_CHARACTERS = 24_000
GUIDANCE_FEEDBACK_MAX = 400
GUIDANCE_DRAFT_MAX = 2000
GUIDANCE_PORTRAIT_CONTEXT_MAX = 120
GUIDANCE_SUMMARY_CONTEXT_MAX = 120
MAX_SURFACE = 24
MAX_IMPLIED = 48
MAX_TACTIC = 10
MAX_SENTIMENT_LABEL = 6
MAX_READING = 80
MAX_STRATEGY = 48
MAX_REPLY_TONE = 8
MAX_REPLY_TEXT = 90
MAX_SELF_SUMMARY = 80
MAX_SELF_POINT = 36
MAX_STRATEGIES = 4
MAX_REPLIES = 3
MAX_SELF_POINTS = 3


def guidance_scope(source_id, reply_options=False, assistant=None):
    """Source-scoped storage key. A prompt revision never mixes with an older one."""
    scope = source_id + ":" + (REPLY_OPTIONS_REVISION if reply_options else GUIDANCE_REVISION)
    if reply_options and assistant is not None:
        identity = [assistant["agentId"], assistant["agentRevision"], assistant["stamp"]]
        scope += ":" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("utf-8")).hexdigest()
    return scope


def valid_reply_options(value):
    """Three distinct complete reply texts, without legacy analysis fields."""
    if (not isinstance(value, dict) or
            set(value) != {"kind", "version", "replies"} or
            value.get("kind") != "reply-options" or
            value.get("version") != REPLY_OPTIONS_REVISION):
        return False
    replies = value.get("replies")
    if not isinstance(replies, list) or len(replies) != 3:
        return False
    seen = set()
    for reply in replies:
        if not isinstance(reply, dict) or set(reply) != {"text"}:
            return False
        text = _text(reply["text"], MAX_REPLY_TEXT)
        if not text or text in seen:
            return False
        seen.add(text)
    return True


def normalize_reply_options(value):
    if not valid_reply_options(value):
        return None
    return {"kind": "reply-options", "version": REPLY_OPTIONS_REVISION,
            "replies": [{"text": reply["text"].strip()} for reply in value["replies"]]}


def valid_guidance_payload(value):
    return valid_reply_options(value) or valid_guidance(value)


def _text(value, maximum):
    if not isinstance(value, str):
        return None
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value.strip()[:maximum]


def _texts(values, maximum, limit):
    if not isinstance(values, list) or len(values) > limit:
        return None
    items = []
    for value in values:
        text = _text(value, maximum)
        if text is None:
            return None
        if text:
            items.append(text)
    return items


def _valid_subtext(item):
    if not isinstance(item, dict):
        return False
    status = item.get("status")
    if not isinstance(status, str) or status not in GUIDANCE_STATUSES:
        return False
    if status != "ok":
        # A terminal reading carries no interpretation, so it may not smuggle one in.
        return set(item) == SUBTEXT_STATUS_FIELDS
    if not set(item) <= SUBTEXT_BODY_FIELDS:
        return False
    if not any(item.get(field) for field in ("surface", "implied", "tactic", "sentiment")):
        return False
    for field, maximum in (("surface", MAX_SURFACE), ("implied", MAX_IMPLIED),
                           ("tactic", MAX_TACTIC)):
        if field in item and _text(item[field], maximum) is None:
            return False
    sentiment = item.get("sentiment")
    if sentiment is not None:
        if not isinstance(sentiment, dict) or set(sentiment) - SENTIMENT_FIELDS:
            return False
        if (not isinstance(sentiment.get("polarity"), str) or
                sentiment.get("polarity") not in GUIDANCE_POLARITIES):
            return False
        if "label" in sentiment and _text(sentiment["label"], MAX_SENTIMENT_LABEL) is None:
            return False
    return True


def _valid_others(others):
    if not isinstance(others, dict) or not set(others) <= OTHERS_FIELDS:
        return False
    reading = _text(others.get("reading"), MAX_READING)
    strategies = _texts(others.get("strategies"), MAX_STRATEGY, MAX_STRATEGIES)
    replies = others.get("replies")
    if not reading or strategies is None or not strategies:
        return False
    if not isinstance(replies, list) or not 1 <= len(replies) <= MAX_REPLIES:
        return False
    for reply in replies:
        if not isinstance(reply, dict) or not set(reply) <= REPLY_FIELDS:
            return False
        if not _text(reply.get("text"), MAX_REPLY_TEXT):
            return False
        if "tone" in reply and not _text(reply["tone"], MAX_REPLY_TONE):
            return False
    return True


def _valid_self(advice_self):
    if not isinstance(advice_self, dict) or not set(advice_self) <= SELF_FIELDS:
        return False
    if not _text(advice_self.get("summary"), MAX_SELF_SUMMARY):
        return False
    for field in ("strengths", "improvements"):
        points = _texts(advice_self.get(field), MAX_SELF_POINT, MAX_SELF_POINTS)
        if not points:
            return False
    return True


def valid_guidance(value):
    """True when one stored guidance payload satisfies the whole contract."""
    if not isinstance(value, dict) or not set(value) <= RESULT_FIELDS | RESULT_OPTIONAL_FIELDS:
        return False
    if value.get("version") != GUIDANCE_REVISION:
        return False
    if not isinstance(value.get("scenario"), str) or value.get("scenario") not in GUIDANCE_SCENARIOS:
        return False
    analyze_self = value.get("analyzeSelf")
    if not isinstance(analyze_self, bool):
        return False
    subtexts = value.get("subtexts")
    # A stored result always explains at least one message; an empty reading list is a
    # failed turn, not a conversation with nothing to read.
    if not isinstance(subtexts, list) or not 1 <= len(subtexts) <= MAX_SUBTEXTS:
        return False
    identifiers = [item.get("id") for item in subtexts if isinstance(item, dict)]
    if (len(identifiers) != len(subtexts) or
            any(not isinstance(identifier, str) for identifier in identifiers) or
            len(set(identifiers)) != len(identifiers)):
        return False
    if any(not isinstance(item["id"], str) or not item["id"] or len(item["id"]) > 200 or
           any(ord(char) < 32 or ord(char) == 127 for char in item["id"]) or
           not _valid_subtext(item) for item in subtexts):
        return False
    advice = value.get("advice")
    if not isinstance(advice, dict) or set(advice) != ADVICE_FIELDS:
        return False
    if not _valid_others(advice["forOthers"]):
        return False
    # The settings switch owns this split: a missing self section must never be
    # rendered as "no issues found", and an unexpected one is a contract violation.
    advice_self = advice["forSelf"]
    if analyze_self:
        if not _valid_self(advice_self):
            return False
    elif advice_self is not None:
        return False
    return True


def normalize_guidance(value):
    """Canonical stored shape, or None when the payload violates the contract."""
    if not valid_guidance(value):
        return None
    subtexts = []
    for item in value["subtexts"]:
        entry = {"id": item["id"], "status": item["status"]}
        for field in ("surface", "implied", "tactic"):
            if item.get(field):
                entry[field] = item[field]
        sentiment = item.get("sentiment")
        if isinstance(sentiment, dict):
            entry["sentiment"] = {key: sentiment[key] for key in ("polarity", "label")
                                  if key in sentiment}
        subtexts.append(entry)
    others = value["advice"]["forOthers"]
    advice_self = value["advice"]["forSelf"]
    return {
        "version": GUIDANCE_REVISION,
        "scenario": value["scenario"],
        "analyzeSelf": value["analyzeSelf"],
        "subtexts": subtexts,
        "advice": {
            "forOthers": {
                "reading": others["reading"],
                "strategies": list(others["strategies"]),
                "replies": [{"text": reply["text"],
                             **({"tone": reply["tone"]} if reply.get("tone") else {})}
                            for reply in others["replies"]],
            },
            "forSelf": None if advice_self is None else {
                "summary": advice_self["summary"],
                "strengths": list(advice_self["strengths"]),
                "improvements": list(advice_self["improvements"]),
            },
        },
    }

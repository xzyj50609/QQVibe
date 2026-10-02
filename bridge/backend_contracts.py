"""Shared backend errors, version keys, and pure domain transformations.

This module must not import services, adapters, or database repositories.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from message_contracts import (API_INSIGHT_REVISION, FINE_LABEL_SCHEMA, api_insight_scope,
                               normalize_api_insight)
from portrait_contracts import API_PORTRAIT_REVISION, api_portrait_scope

LOCAL_SOURCE_ID = "local:laya"


class ModelSourceUnavailable(RuntimeError):
    """A source operation could not complete without changing active inference."""

MODEL_CONNECTOR_ERRORS = frozenset({
    "invalid-url", "invalid-request", "context-too-long", "auth", "rate-limit", "timeout", "unsupported",
    "network", "provider-error", "invalid-output", "response-too-large", "empty-response",
})


API_PORTRAIT_PIECE_CHARS = 1000


API_PORTRAIT_BATCH_ITEMS = 20_000


API_PORTRAIT_MAX_WIRE_CHARS = 600_000


API_PORTRAIT_MAX_BATCHES = 4096


API_PORTRAIT_INVENTORY_CACHE_BYTES = 2 * 1024 * 1024


API_MODEL_RETRY_MAX = 10


API_MODEL_RETRY_SECONDS = 5


API_MODEL_RETRYABLE = frozenset({
    "invalid-output", "invalid-portrait", "timeout",
    "empty-response", "response-too-large", "network", "provider-error", "rate-limit",
})

# Message insight calls follow OpenCode's shorter transient-error policy. Format
# and provider-parameter failures terminate immediately instead of looping.
API_INSIGHT_RETRY_MAX = 5
API_INSIGHT_RETRY_SECONDS = 2
API_INSIGHT_RETRYABLE = frozenset({"timeout", "network", "rate-limit"})


def valid_api_portrait(value):
    if not isinstance(value, dict) or set(value) != {
            "summary", "communication", "emotionExpression", "interactionPreferences",
            "topics", "patterns", "boundaries", "uncertain",
            "affinity", "mbtiAxes", "traits"}:
        return False
    if any(not isinstance(value[key], str) or len(value[key]) > maximum
           for key, maximum in (("summary", 240), ("communication", 120),
                                ("emotionExpression", 120), ("interactionPreferences", 120))):
        return False
    if not all(isinstance(value[key], list) and len(value[key]) <= 6 and
               all(isinstance(item, str) and 0 < len(item) <= maximum for item in value[key])
               for key, maximum in (("topics", 30), ("patterns", 80),
                                    ("boundaries", 80), ("uncertain", 80))):
        return False
    def score(number):
        return number is None or (type(number) is int and 0 <= number <= 100)
    axes = value["mbtiAxes"]
    traits = value["traits"]
    return (score(value["affinity"]) and isinstance(axes, dict) and
            set(axes) == {"EI", "SN", "TF", "JP"} and all(map(score, axes.values())) and
            isinstance(traits, dict) and
            set(traits) == {"socialEnergy", "humor", "composure", "initiative", "care", "affection"} and
            all(map(score, traits.values())))


def empty_api_portrait():
    return {"summary": "", "communication": "", "emotionExpression": "",
            "interactionPreferences": "", "topics": [], "patterns": [],
            "boundaries": [], "uncertain": [], "affinity": None,
            "mbtiAxes": {key: None for key in ("EI", "SN", "TF", "JP")},
            "traits": {key: None for key in ("socialEnergy", "humor", "composure",
                                              "initiative", "care", "affection")}}


def api_portrait_wire_chars(context_tokens):
    if type(context_tokens) is not int or not 4096 <= context_tokens <= 1000000:
        raise ModelSourceUnavailable("context-size-required")
    # Reserve system/summary/output tokens; UTF-8 and JSON overhead are included
    # in the measured wire length below. The conservative ratio avoids promising
    # that a character is exactly one provider token.
    return min(API_PORTRAIT_MAX_WIRE_CHARS, max(1024, (context_tokens - 2048) * 55 // 100))


def api_portrait_plan(pieces, wire_chars):
    if type(wire_chars) is not int or not 1024 <= wire_chars <= API_PORTRAIT_MAX_WIRE_CHARS:
        raise ValueError("invalid portrait context budget")
    if not pieces:
        return []
    weights = [len(json.dumps({key: item[key] for key in ("id", "sender", "target", "text")},
                              ensure_ascii=False, separators=(",", ":"))) + 1 for item in pieces]
    groups = []
    start = total = 0
    for index, weight in enumerate(weights):
        if weight > wire_chars:
            raise ModelSourceUnavailable("context-too-long")
        if index > start and (index - start >= API_PORTRAIT_BATCH_ITEMS or
                              total + weight > wire_chars):
            groups.append((start, index))
            start, total = index, 0
        total += weight
    groups.append((start, len(pieces)))
    if len(groups) > API_PORTRAIT_MAX_BATCHES:
        raise ModelSourceUnavailable("context-too-long")
    return [last for _first, last in groups]


API_PORTRAIT_COUNT_KEYS = ("messageCount", "textCount", "targetTextCount",
                           "totalChars", "pieceCount")


def api_portrait_add_counts(base, delta):
    if not all(type(base.get(key)) is int and type(delta.get(key)) is int and
               base[key] >= 0 and delta[key] >= 0 for key in API_PORTRAIT_COUNT_KEYS):
        raise ValueError("invalid API portrait inventory")
    return {key: base[key] + delta[key] for key in API_PORTRAIT_COUNT_KEYS}


def api_portrait_tail_hashes(rows):
    """A compact chain over the remaining frozen rows, including non-text rows."""
    count = len(rows) // 32
    tails = bytearray((count + 1) * 32)
    tails[count * 32:] = hashlib.sha256(b"").digest()
    for index in range(count - 1, -1, -1):
        start = index * 32
        tails[start:start + 32] = hashlib.sha256(
            rows[start:start + 32] + tails[start + 32:start + 64]).digest()
    return tails


def api_portrait_resume_anchor(piece, piece_offset, batch_index, tails):
    after = piece["_sort"] if piece["_last"] else piece["_before"]
    return {"batchIndex": batch_index, "pieceOffset": piece_offset,
            "after": list(after) if after is not None else None,
            "partialSort": list(piece["_sort"]) if not piece["_last"] else None,
            "skipPieces": 0 if piece["_last"] else piece["_pieceIndex"] + 1,
            "tailHash": tails[(piece["_rowIndex"] + int(piece["_last"])) * 32:
                              (piece["_rowIndex"] + int(piece["_last"]) + 1) * 32].hex()}


def model_source_failure(error, fallback):
    code = str(error)
    return ModelSourceUnavailable(code if code in MODEL_CONNECTOR_ERRORS else fallback)


ROOT = Path(__file__).resolve().parents[1]


MOODS = {"happy": "(＾▽＾)", "affectionate": "(❤´艸｀❤)", "neutral": "(￣▽￣)",
         "amused": "(≧▽≦)", "sad": "(╥﹏╥)", "anxious": "(；´д｀)", "angry": "(｀皿´)"}


AXES = {"EI": ("E", "I"), "SN": ("S", "N"), "TF": ("T", "F"), "JP": ("J", "P")}


MIN_PERSONALITY_MESSAGES = 100


MIN_AXIS_EVIDENCE = 30


MIN_AXIS_MARGIN = .2


MBTI_SOURCES = [
    "https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/",
    "https://www.themyersbriggs.com/en-US/Products-and-Services/Myers-Briggs",
]


SYSTEM_NAMES = {"filehelper": "文件传输助手", "newsapp": "腾讯新闻", "brandsessionholder": "公众号消息"}


MAX_ISSUED_IMAGES = 2048


MAX_IMAGE_BYTES = 8 * 1024 * 1024


FORECAST_CACHE_LIMIT = 64


FORECAST_SOURCE_WINDOW = 16


PROFILE_METADATA_CACHE_LIMIT = 64


API_JOB_CACHE_LIMIT = 256


GROUNDED_INTENT_EVIDENCE = {
    "greet": {"greeting_phrase"}, "thank": {"thanks_phrase"},
    "confirm": {"short_acknowledgement"}, "inspect": {"first_person_inspection"},
    "agree": {"explicit_acceptance"}, "reject": {"explicit_refusal", "contextual_deferral"},
    "deny": {"contextual_denial"},
    "invite": {"inclusive_invitation"}, "ask_question": {"answer_seeking_question"},
    "seek_help": {"action_request"},
    "suggest_action": {"advice_marker", "negative_imperative", "imperative_adjustment", "delegated_action"},
    "plan": {"first_person_intention"}, "correct": {"explicit_correction"},
    "explain": {"causal_explanation", "process_explanation"}, "complain": {"negative_evaluation"},
    "status_report": {"progress_statement"}, "share_news": {"sharing_announcement"},
}


QUOTED_REPLY_TYPE = (57 << 32) | 49


SESSION_PREVIEWS = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]",
                    48: "[位置]", 49: "[文件/链接/卡片]", 11000: "[表情]"}


class ForecastRequestError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AccountUnavailableError(RuntimeError):
    """The unique live WeChat account or its cache-only database is not ready."""

    def __init__(self):
        super().__init__("当前账号未就绪")


class AccountChangedError(RuntimeError):
    """A request or queued analysis outlived the account that supplied its input."""

    def __init__(self):
        super().__init__("当前账号已变化")


class MessagesUnavailableError(RuntimeError):
    def __init__(self):
        super().__init__("当前消息尚未就绪")


def validate_personality_evidence(evidence):
    if evidence is None:
        return None
    if not isinstance(evidence, dict) or set(evidence) != set(AXES):
        raise RuntimeError("invalid personality evidence axes")
    for axis, (left, right) in AXES.items():
        distribution = evidence[axis]
        if not isinstance(distribution, dict) or set(distribution) != {left, right, "insufficient"}:
            raise RuntimeError("invalid personality evidence distribution")
        probabilities = list(distribution.values())
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities) or sum(probabilities) <= 0:
            raise RuntimeError("invalid personality evidence probability")
    return evidence


def infer_mbti(results, version):
    axes = {}
    supported_messages = 0
    axis_totals = {axis: [0.0, 0.0, 0, 0] for axis in AXES}
    for result in results:
        evidence = result.get("personalityEvidence")
        supported = False
        for axis, (left, right) in AXES.items():
            totals = axis_totals[axis]
            distribution = evidence[axis] if evidence else None
            if distribution is None or distribution["insufficient"] >= max(distribution[left], distribution[right]):
                totals[3] += 1
                continue
            totals[0] += distribution[left]
            totals[1] += distribution[right]
            totals[2] += 1
            supported = True
        if supported:
            supported_messages += 1

    return mbti_from_totals(len(results), supported_messages, axis_totals, version)


def mbti_from_totals(eligible, supported_messages, axis_totals, version):
    axes = {}
    preferences = []
    for axis, (left, right) in AXES.items():
        left_total, right_total, evidence_count, insufficient_count = axis_totals[axis]
        mass = left_total + right_total
        left_share = left_total / mass if evidence_count >= MIN_AXIS_EVIDENCE and mass > 0 else None
        right_share = right_total / mass if left_share is not None else None
        axes[axis] = {"left": left, "right": right, "leftShare": left_share,
                      "rightShare": right_share, "evidenceCount": evidence_count,
                      "insufficientCount": insufficient_count}
        if left_share is None or abs(left_share - right_share) < MIN_AXIS_MARGIN:
            preferences.append(None)
        else:
            preferences.append(left if left_share > right_share else right)

    inferred_type = "".join(preferences) if eligible >= MIN_PERSONALITY_MESSAGES and all(preferences) else None
    return {"basis": "chat-inference", "theory": "MBTI preferences", "version": version,
            "eligibleMessages": eligible, "supportedMessages": supported_messages,
            "minMessages": MIN_PERSONALITY_MESSAGES, "axes": axes, "sources": MBTI_SOURCES,
            "type": inferred_type,
            "status": "estimated" if inferred_type else "insufficient" if eligible < MIN_PERSONALITY_MESSAGES else "partial"}


def affinity(scores):
    """The linear-recency aggregate in electron/native/aggregate.ts, oldest first."""
    count = len(scores)
    if not count:
        return None
    if count == 1:
        weighted = scores[0]
    else:
        weighted = (.5 * sum(scores) + .5 * sum(index * score for index, score in enumerate(scores)) / (count - 1)) / (.75 * count)
    return int(50 + 50 * max(-1, min(1, weighted)) + .5)


def affinity_from_progress(progress):
    count = progress["scoreCount"]
    if not count:
        return None
    weighted = (progress["scoreSum"] if count == 1 else
                (.5 * progress["scoreSum"] + .5 * progress["scoreWeighted"] / (count - 1)) /
                (.75 * count))
    return int(50 + 50 * max(-1, min(1, weighted)) + .5)


def mood_from_progress(progress):
    count = progress["moodCount"]
    if not count:
        return None
    totals = {raw: (values["sum"] if count == 1 else
                    .5 * values["sum"] + .5 * values["weighted"] / (count - 1))
              for raw, values in progress["mood"].items()}
    dominant = max(totals, key=totals.get)
    return {"label": progress["mood"][dominant]["label"], "rawLabel": dominant,
            "kaomoji": MOODS.get(dominant), "sampleCount": count,
            "scope": "analyzed-history"}


def scope_rank(limit):
    return math.inf if limit == "all" else limit


class MessageWindow(list):
    def __init__(self, items, has_more_before):
        super().__init__(items)
        self.has_more_before = has_more_before


class MessageWindowBatch(dict):
    def __init__(self, windows, has_more_before):
        super().__init__(windows)
        self.has_more_before = has_more_before

"""API batch judgments accumulated with the existing local Laya portrait rules.

This module has no IO. Its returned value belongs in the API source-scoped resume
checkpoint, committed atomically with that checkpoint's message cursor. Free-form
legacy API portrait numbers are deliberately not accepted as statistics.
"""
from __future__ import annotations

from copy import deepcopy
import math

from batch_state import _combined_result, _merge
from backend_contracts import (affinity_from_progress, mbti_from_totals,
                               mood_from_progress, validate_personality_evidence)
from profile_signals import (STYLE_LABELS, keyword_counts, keywords_from_counts,
                             summary_from_aggregate, validate_style_evidence)
from profile_state import empty_state, traits_from_state

API_PORTRAIT_STATISTICS_VERSION = 1
MAX_COUNT = 9007199254740991
MAX_PENDING_CHARS = 4_000_000
SIGNAL_KEYS = frozenset({"emotion", "intent", "intentBroad", "score",
                         "styleEvidence", "personalityEvidence"})


def empty_statistics(classifier_version="api-local-rules-v1"):
    if not _text(classifier_version):
        raise ValueError("invalid API portrait classifier version")
    return {"version": API_PORTRAIT_STATISTICS_VERSION,
            "classifierVersion": classifier_version,
            "state": {**empty_state(), "batchCount": 0}, "pending": None}


def _number(value, low=0, high=MAX_COUNT):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _count(value, high=MAX_COUNT):
    return type(value) is int and 0 <= value <= high


def _text(value, maximum=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _position(value, *, message=False):
    return (isinstance(value, (list, tuple)) and len(value) == (4 if message else 3)
            and _count(value[0]) and _text(value[1]) and _count(value[2])
            and (not message or _text(value[3])))


def _signal(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not SIGNAL_KEYS <= value.keys():
        raise ValueError("invalid API portrait batch signal")
    for field in ("emotion", "intent", "intentBroad"):
        entries = value[field]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
            raise ValueError("invalid API portrait distribution")
        for entry in entries:
            if (not isinstance(entry, dict) or not _text(entry.get("label"), 120) or
                    not _number(entry.get("probability"), 0, 1) or
                    "rawLabel" in entry and not _text(entry["rawLabel"], 120)):
                raise ValueError("invalid API portrait probability")
        mass = sum(entry["probability"] for entry in entries)
        # The local router retains only selected branches and deliberately does
        # not renormalize their joint probability mass. Broad intent is complete.
        if not (0 < mass <= 1.01) or field == "intentBroad" and mass < .99:
            raise ValueError("invalid API portrait probability mass")
    if not _number(value["score"], -1, 1):
        raise ValueError("invalid API relationship score")
    if value["styleEvidence"] is None:
        raise ValueError("missing API portrait classification")
    validate_style_evidence(value["styleEvidence"])
    validate_personality_evidence(value["personalityEvidence"])
    return {key: deepcopy(value[key]) for key in SIGNAL_KEYS}


def validate_batch_signal(result, pieces):
    """Validate provider output only; this is the sole paid-retry boundary.

    The backend owns ``pieces``. Their structure, cursor continuity and saved
    statistics must be checked by append_batch *outside* the model retry loop.
    Looking at target text here only establishes whether a null provider result
    is legal; deterministic piece errors must not be classified as model errors.
    """
    signal = _signal(result)
    input_pieces = pieces if isinstance(pieces, list) else ()
    if signal is None and any(isinstance(item, dict) and item.get("target") is True and
                              isinstance(item.get("text"), str) and item["text"].strip()
                              for item in input_pieces):
        raise ValueError("missing API portrait target classification")
    return signal


def valid_statistics(value, classifier_version=None, *, allow_historical_pending=False):
    """Reject incompatible or corrupted saved state; never seed from old numbers."""
    if (not isinstance(value, dict) or set(value) != {"version", "classifierVersion", "state", "pending"} or
            type(value["version"]) is not int or value["version"] != API_PORTRAIT_STATISTICS_VERSION or
            not _text(value["classifierVersion"]) or
            classifier_version is not None and value["classifierVersion"] != classifier_version):
        return False
    state = value["state"]
    if not isinstance(state, dict) or set(state) != set(empty_state()) | {"batchCount"}:
        return False
    for key in ("count", "targetCount", "scoreCount", "moodCount", "styleCount", "supported", "batchCount"):
        if not _count(state[key]):
            return False
    target_count = state["targetCount"]
    if (target_count > state["count"] or any(state[key] > target_count
            for key in ("scoreCount", "moodCount", "styleCount", "supported")) or
            state["latest"] is not None and not _position(state["latest"], message=True)):
        return False
    score_count = state["scoreCount"]
    rank_mass = score_count * max(0, score_count - 1) / 2
    tolerance = max(1e-9, score_count * 1e-9)
    if (not _number(state["scoreSum"], -score_count-tolerance, score_count+tolerance) or
            not _number(state["scoreWeighted"], -rank_mass-tolerance, rank_mass+tolerance)):
        return False
    for field in ("emotion", "intent", "broad", "words"):
        entries = state[field]
        if not isinstance(entries, dict) or field != "words" and len(entries) > 4096:
            return False
        if any(not _text(label, 120) or
               not (_count(mass) if field == "words" else _number(mass, 0, target_count+tolerance))
               for label, mass in entries.items()):
            return False
    mood_count = state["moodCount"]
    if not isinstance(state["mood"], dict) or len(state["mood"]) > 4096:
        return False
    for raw, mood in state["mood"].items():
        if (not _text(raw, 120) or not isinstance(mood, dict) or set(mood) != {"label", "sum", "weighted"} or
                not _text(mood["label"], 120) or not _number(mood["sum"], 0, mood_count+tolerance) or
                not _number(mood["weighted"], 0, mood_count*max(0, mood_count-1)/2+tolerance)):
            return False
    if (bool(state["mood"]) != bool(mood_count) or not isinstance(state["style"], dict) or
            set(state["style"]) != set(STYLE_LABELS) or
            any(not _number(mass, 0, state["styleCount"]+tolerance) for mass in state["style"].values())):
        return False
    if not isinstance(state["axes"], dict) or set(state["axes"]) != {"EI", "SN", "TF", "JP"}:
        return False
    for axis in state["axes"].values():
        if (not isinstance(axis, list) or len(axis) != 4 or
                not _count(axis[2], target_count) or not _count(axis[3], target_count) or
                axis[2] + axis[3] > target_count or
                any(not _number(mass, 0, axis[2]+tolerance) for mass in axis[:2])):
            return False
    pending = value["pending"]
    if pending is None:
        return True
    if (not isinstance(pending, dict) or set(pending) != {
            "messageId", "speaker", "position", "nextPieceIndex", "text", "signalChars", "weightedResult"} or
            not _text(pending["messageId"]) or not _text(pending["speaker"]) or
            not _position(pending["position"]) or not _count(pending["nextPieceIndex"], MAX_PENDING_CHARS) or
            pending["nextPieceIndex"] < 1 or not isinstance(pending["text"], str) or
            not 0 < len(pending["text"]) <= MAX_PENDING_CHARS or
            not _count(pending["signalChars"], len(pending["text"])) or
            (pending["weightedResult"] is None) != (pending["signalChars"] == 0)):
        return False
    if not allow_historical_pending and state["latest"] is not None and tuple(pending["position"]) <= tuple(state["latest"][:3]):
        return False
    try:
        _signal(pending["weightedResult"])
    except (ValueError, RuntimeError, KeyError, TypeError):
        return False
    return True


def append_batch(statistics, result, pieces, *, is_group=False, subject):
    """Return new statistics; retries against the same old checkpoint are pure.

    ``pieces`` are backend-generated chronological API pieces (not model output).
    Completion, target identity and coverage come only from those input records.
    The caller atomically saves this return value with the corresponding cursor.
    """
    if not valid_statistics(statistics):
        raise ValueError("invalid API portrait statistics")
    if type(is_group) is not bool or not isinstance(subject, str) or not is_group and not subject:
        raise ValueError("invalid API portrait statistics subject")
    if not isinstance(pieces, list) or len(pieces) > 20_000:
        raise ValueError("invalid API portrait pieces")
    signal = validate_batch_signal(result, pieces)
    updated = deepcopy(statistics)
    state = updated["state"]
    state["batchCount"] += int(signal is not None)
    # Preserve the local store's fast-batch arithmetic too: applying one shared
    # judgment N times can accumulate enough floating error to round affinity a
    # point differently from its one-block merge.
    fast_batch = (signal is not None and updated["pending"] is None and
                  all(isinstance(item, dict) and (not item.get("target") or
                      item.get("_pieceIndex") == 0 and item.get("_last") is True) for item in pieces))
    fast_targets = []
    previous_position = None
    for piece in pieces:
        if (not isinstance(piece, dict) or not _text(piece.get("messageId")) or
                not _text(piece.get("speaker")) or not _position(piece.get("_sort")) or
                not isinstance(piece.get("text"), str) or not 0 < len(piece["text"]) <= MAX_PENDING_CHARS or
                type(piece.get("target")) is not bool or type(piece.get("_last")) is not bool or
                piece.get("sender") not in ("SELF", "OTHER") or
                not _count(piece.get("_pieceIndex"), MAX_PENDING_CHARS)):
            raise ValueError("invalid API portrait piece")
        position = tuple(piece["_sort"])
        if previous_position is not None and position < previous_position:
            raise ValueError("API portrait pieces out of order")
        previous_position = position
        pending = updated["pending"]
        if not piece["target"]:
            if pending is not None:
                raise ValueError("API portrait target fragment skipped")
            if is_group and not subject and piece["sender"] == "SELF" and piece["_last"]:
                state["count"] += 1  # Same group coverage rule as BatchStateStore.commit.
            continue
        if fast_batch:
            latest = fast_targets[-1]["_sort"] if fast_targets else state["latest"]
            if latest is not None and position <= tuple(latest[:3]):
                raise ValueError("API portrait target did not advance")
            fast_targets.append(piece)
            continue
        if pending is None:
            if piece["_pieceIndex"] != 0 or (state["latest"] is not None and
                                             position <= tuple(state["latest"][:3])):
                raise ValueError("API portrait target did not advance")
            pending = {"messageId": piece["messageId"], "speaker": piece["speaker"],
                       "position": list(position), "nextPieceIndex": 0, "text": "",
                       "signalChars": 0, "weightedResult": None}
        if (pending["messageId"] != piece["messageId"] or pending["speaker"] != piece["speaker"] or
                tuple(pending["position"]) != position or pending["nextPieceIndex"] != piece["_pieceIndex"]):
            raise ValueError("non-contiguous API portrait target fragments")
        pending["text"] += piece["text"]
        if len(pending["text"]) > MAX_PENDING_CHARS:
            raise ValueError("API portrait pending message too large")
        pending["weightedResult"] = _combined_result([
            (pending["signalChars"], pending["weightedResult"]), (len(piece["text"]), signal)])
        pending["signalChars"] += len(piece["text"]) if signal is not None else 0
        pending["nextPieceIndex"] += 1
        if piece["_last"]:
            effective = pending["weightedResult"]
            if effective is not None:
                _merge(state, effective, 1, (*position, piece["messageId"]),
                       dict(keyword_counts([pending["text"]])), is_group, subject)
            updated["pending"] = None
        else:
            updated["pending"] = pending
    if fast_targets:
        words = {}
        for item in fast_targets:
            for word, count in keyword_counts([item["text"]]).items():
                words[word] = words.get(word, 0) + count
        last = fast_targets[-1]
        _merge(state, _combined_result([(1, signal)]), len(fast_targets),
               (*last["_sort"], last["messageId"]), words, is_group, subject)
    if not valid_statistics(updated):
        raise ValueError("invalid accumulated API portrait statistics")
    return updated


def profile_from_statistics(statistics, version, *, is_group=False, subject):
    """Use exactly the local portrait transformations for API-scoped signals."""
    if not valid_statistics(statistics):
        raise ValueError("invalid API portrait statistics")
    if type(is_group) is not bool or not isinstance(subject, str) or not is_group and not subject:
        raise ValueError("invalid API portrait statistics subject")
    state = statistics["state"]
    distributions = {field: [{"label": label, "probability": mass / state["count"]}
                             for label, mass in state[field].items()] if state["count"] else []
                     for field in ("emotion", "intent")}
    mood = mood_from_progress(state)
    keywords = keywords_from_counts(state["words"])
    inference = None if is_group and not subject else mbti_from_totals(
        state["targetCount"], state["supported"], state["axes"], statistics["classifierVersion"])
    return {"affinity": None if is_group or subject == "self" else affinity_from_progress(state), **distributions,
            "keywords": keywords, "mood": mood, "mbti": inference["type"] if inference else None,
            "mbtiInference": inference, "traits": traits_from_state(state), "traitsBasis": "chat-behaviour",
            "summary": summary_from_aggregate(state["targetCount"], state["broad"], mood, keywords,
                                              is_group and not subject),
            "suggestions": [], "portraitCount": state["targetCount"], "analyzedCount": state["count"]}

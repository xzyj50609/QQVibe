"""Pure validation of one Node analyzer result for a single message.

Two entry points preserve the existing order and error text used by
``Backend._analyze_item``:

- ``validate_fine_result``: the finer per-message label path (``messageLabelsOnly``).
- ``validate_portrait_result``: the ordinary single-message analysis path.

Neither entry point performs IO, locking, saving, scoring or metrics; ``side`` is the
target message side and only enforces the relationship-score requirement. The fine
path never calls the portrait entry; both share the low-level structural checks.
The legacy fine relationship-score requirement is kept here for compatibility and is
revisited by a later semantics batch, not by this structural split.
"""
from __future__ import annotations

import math

from backend_contracts import FINE_LABEL_SCHEMA, GROUNDED_INTENT_EVIDENCE, validate_personality_evidence
from profile_signals import validate_style_evidence


def _check_version(response, version):
    if response.get("analysisVersion") != version:
        raise RuntimeError("analysis response version mismatch")


def _check_schema(response, fine):
    label_schema = response.get("labelSchema")
    if fine and label_schema != FINE_LABEL_SCHEMA:
        raise RuntimeError("fine label schema mismatch")
    if label_schema is not None and not isinstance(label_schema, str):
        raise RuntimeError("invalid label schema")
    return label_schema


def _check_grounded(response, fine):
    if fine:
        if "groundedIntent" not in response:
            raise RuntimeError("missing grounded intent")
        grounded_intent = response["groundedIntent"]
        if grounded_intent is not None and (
            not isinstance(grounded_intent, dict) or
            set(grounded_intent) != {"label", "evidenceKind"} or
            not isinstance(grounded_intent.get("label"), str) or
            not isinstance(grounded_intent.get("evidenceKind"), str) or
            grounded_intent["evidenceKind"] not in
            GROUNDED_INTENT_EVIDENCE.get(grounded_intent["label"], set())
        ):
            raise RuntimeError("invalid grounded intent")
        return grounded_intent
    if "groundedIntent" in response:
        raise RuntimeError("grounded intent in portrait result")
    return None


def _check_distributions(response):
    for field in ("emotion", "intent"):
        distribution = response.get(field)
        if not isinstance(distribution, list) or not distribution or any(
            not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or
            not isinstance(entry.get("probability"), (int, float)) or
            not math.isfinite(entry["probability"]) or not 0 <= entry["probability"] <= 1
            for entry in distribution
        ):
            raise RuntimeError("invalid model " + field)
    broad = response.get("intentBroad") or []
    if not isinstance(broad, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or
        not isinstance(entry.get("probability"), (int, float)) or
        not math.isfinite(entry["probability"]) or not 0 <= entry["probability"] <= 1
        for entry in broad
    ):
        raise RuntimeError("invalid broad intent")
    expression = response.get("expression", [])
    if not isinstance(expression, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or
        not isinstance(entry.get("probability"), (int, float)) or
        not math.isfinite(entry["probability"]) or not 0 <= entry["probability"] <= 1
        for entry in expression
    ):
        raise RuntimeError("invalid expression distribution")
    playful_intent = response.get("playfulIntent", [])
    if not isinstance(playful_intent, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or
        not entry["label"].startswith("playful:") or
        not isinstance(entry.get("probability"), (int, float)) or isinstance(entry["probability"], bool) or
        not math.isfinite(entry["probability"]) or not 0 <= entry["probability"] <= 1
        for entry in playful_intent
    ):
        raise RuntimeError("invalid playful intent distribution")
    return broad, expression, playful_intent


def _build_result(response, version, side, fine, relationship_applicable=True):
    _check_version(response, version)
    label_schema = _check_schema(response, fine)
    grounded_intent = _check_grounded(response, fine)
    broad, expression, playful_intent = _check_distributions(response)
    result = {field: response[field] for field in
              ("emotion", "intent", "emotionLabel", "intentLabel", "emotionP", "intentP", "analysisVersion")}
    result["intentBroad"] = broad
    if label_schema is not None:
        result["labelSchema"] = label_schema
    if fine:
        result["groundedIntent"] = grounded_intent
    result["expression"] = expression
    result["playfulIntent"] = playful_intent
    result["styleEvidence"] = validate_style_evidence(response.get("styleEvidence"))
    result["personalityEvidence"] = validate_personality_evidence(response.get("personalityEvidence"))
    # Legacy fine results still require a bounded relationship score for OTHER
    # targets. This is preserved verbatim; the semantics batch will revisit it.
    score = response.get("score")
    if relationship_applicable and side == "other" and (not isinstance(score, (float, int)) or not -1 <= score <= 1):
        raise RuntimeError("missing relationship score")
    if not relationship_applicable and score is not None:
        raise RuntimeError('group relationship score is not applicable')
    result.update({"state": "done", "score": score if side == "other" and relationship_applicable else None})
    return result


def validate_fine_result(response, version, side, *, relationship_applicable=True):
    """Validate one fine (``messageLabelsOnly``) target result and build its record."""
    return _build_result(response, version, side, True,relationship_applicable)


def validate_portrait_result(response, version, side):
    """Validate one ordinary single-message analysis result and build its record."""
    return _build_result(response, version, side, False)

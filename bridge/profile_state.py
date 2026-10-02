"""Incremental sufficient statistics for a persisted, target-scoped portrait."""
from profile_signals import STYLE_LABELS


def empty_state():
    return {"count": 0, "targetCount": 0, "scoreCount": 0, "scoreSum": 0.0,
            "scoreWeighted": 0.0, "moodCount": 0, "mood": {}, "latest": None,
            "emotion": {}, "intent": {}, "broad": {}, "words": {},
            "styleCount": 0, "style": {key: 0.0 for key in STYLE_LABELS},
            "axes": {axis: [0.0, 0.0, 0, 0] for axis in ("EI", "SN", "TF", "JP")},
            "supported": 0}


def add_result(state, result, score, side, position, words, tails=()):
    """Append normally in O(new evidence); tails are only for out-of-order backfill."""
    state["count"] += 1
    for field in ("emotion", "intent"):
        for entry in result.get(field) or []:
            label = entry["label"]
            state[field][label] = state[field].get(label, 0.0) + entry["probability"]
    if side != "other":
        return
    state["targetCount"] += 1
    tail_scores = [value for _result, value in tails if value is not None]
    tail_emotions = [item.get("emotion") for item, _score in tails if item.get("emotion")]
    if score is not None:
        state["scoreWeighted"] += (state["scoreCount"] - len(tail_scores)) * score + sum(tail_scores)
        state["scoreSum"] += score
        state["scoreCount"] += 1
    emotion = result.get("emotion") or []
    if emotion:
        rank = state["moodCount"] - len(tail_emotions)
        for tail in tail_emotions:
            for entry in tail:
                raw = entry.get("rawLabel") or entry["label"]
                state["mood"][raw]["weighted"] += entry["probability"]
        for entry in emotion:
            raw = entry.get("rawLabel") or entry["label"]
            values = state["mood"].setdefault(raw, {"label": entry["label"], "sum": 0.0, "weighted": 0.0})
            values["sum"] += entry["probability"]
            values["weighted"] += rank * entry["probability"]
            values["label"] = entry["label"]
        state["moodCount"] += 1
    if state["latest"] is None or tuple(position) > tuple(state["latest"]):
        state["latest"] = list(position)
    for word, count in words.items():
        state["words"][word] = state["words"].get(word, 0) + count
    for entry in result.get("intentBroad") or []:
        label = entry["label"]
        state["broad"][label] = state["broad"].get(label, 0.0) + entry["probability"]
    evidence = result.get("styleEvidence")
    if evidence:
        state["styleCount"] += 1
        for key in STYLE_LABELS:
            state["style"][key] += evidence[key]
    evidence = result.get("personalityEvidence")
    supported = False
    for axis, totals in state["axes"].items():
        left, right = axis
        distribution = evidence[axis] if evidence else None
        if distribution is None or distribution["insufficient"] >= max(distribution[left], distribution[right]):
            totals[3] += 1
            continue
        totals[0] += distribution[left]
        totals[1] += distribution[right]
        totals[2] += 1
        supported = True
    state["supported"] += int(supported)


def traits_from_state(state):
    count = state["styleCount"]
    return [{"key": key, "label": label, "val": int(100 * state["style"][key] / count + .5),
             "sampleCount": count} for key, label in STYLE_LABELS.items()] if count else []

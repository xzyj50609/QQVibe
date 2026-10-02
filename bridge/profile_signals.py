"""Deterministic summaries of cached, target-scoped model signals."""
from __future__ import annotations

import math
import re
from collections import Counter

STYLE_LABELS = {
    "socialEnergy": "表达活力", "humor": "幽默表达", "composure": "情绪平和",
    "initiative": "话题主动", "care": "关怀支持", "affection": "亲近表达",
}
STOPWORDS = {"这个", "那个", "我们", "你们", "他们", "自己", "就是", "可以", "一下", "然后", "不是", "没有", "什么", "怎么", "真的", "还是", "还有"}


def validate_style_evidence(evidence):
    if evidence is None:
        return None
    if not isinstance(evidence, dict) or set(evidence) != set(STYLE_LABELS):
        raise RuntimeError("invalid style evidence axes")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or not 0 <= value <= 1 for value in evidence.values()):
        raise RuntimeError("invalid style evidence probability")
    return evidence


def historical_mood(rows, kaomoji):
    signals = []
    for _, result, _, _ in rows:
        emotion = result.get("emotion") or []
        if emotion:
            signals.append(emotion)
    if not signals:
        return None
    totals = {}
    labels = {}
    count = len(signals)
    for index, emotion in enumerate(signals):
        weight = 1 if count == 1 else .5 + .5 * index / (count - 1)
        for entry in emotion:
            raw = entry.get("rawLabel") or entry["label"]
            totals[raw] = totals.get(raw, 0) + weight * entry["probability"]
            labels[raw] = entry["label"]
    dominant = max(totals, key=totals.get)
    return {"label": labels[dominant], "rawLabel": dominant, "kaomoji": kaomoji.get(dominant),
            "sampleCount": count, "scope": "analyzed-history"}


def style_traits(rows):
    values = {key: [] for key in STYLE_LABELS}
    for _, result, _, _ in rows:
        evidence = result.get("styleEvidence")
        if evidence is None:
            continue
        validate_style_evidence(evidence)
        for key in STYLE_LABELS:
            values[key].append(evidence[key])
    return [{"key": key, "label": label, "val": int(100 * sum(values[key]) / len(values[key]) + .5),
             "sampleCount": len(values[key])}
            for key, label in STYLE_LABELS.items() if values[key]]


def keyword_counts(texts):
    try:
        import jieba
    except ImportError:
        jieba = None
    counts = Counter()
    for text in texts:
        tokens = jieba.lcut(text) if jieba else re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]{3,}", text)
        for token in tokens:
            word = token.strip().lower()
            if word in STOPWORDS or (not re.fullmatch(r"[\u4e00-\u9fff]{2,8}|[a-z]{3,24}", word)):
                continue
            counts[word] += 1
    return counts


def keywords_from_counts(counts):
    return [{"word": word, "count": count} for word, count in
            sorted(counts.items(), key=lambda item: (-item[1], item[0])) if count >= 2][:6]


def keywords_from_texts(texts):
    return keywords_from_counts(keyword_counts(texts))


def summary_from_signals(rows, mood, keywords, group=False):
    if not rows:
        return None
    broad = Counter()
    for _, result, _, _ in rows:
        for entry in result.get("intentBroad") or []:
            broad[entry["label"]] += entry["probability"]
    return summary_from_aggregate(len(rows), broad, mood, keywords, group)


def summary_from_aggregate(count, broad, mood, keywords, group=False):
    if not count:
        return None
    subject = "群内成员" if group else "该联系人"
    parts = [f"已分析{count}条{subject}消息"]
    if broad:
        parts.append(f"常见交流意图为{max(broad, key=broad.get)}")
    if mood:
        parts.append(f"情绪信号以{mood['label']}为主")
    if keywords:
        parts.append("常见用词有" + "、".join(item["word"] for item in keywords[:3]))
    return "，".join(parts) + "。"

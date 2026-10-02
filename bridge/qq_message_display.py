"""Bounded QQ display metadata. No media IO and no change to authored text."""
import json

MAX_RAW_CHARS = 2_000_000
MAX_QUOTE_CHARS = 4000
RAW_PARTS = {"picElement": "image", "pttElement": "audio", "fileElement": "file",
             "videoElement": "video", "faceElement": "face", "marketFaceElement": "face"}
EXPORT_PARTS = {"image": "image", "audio": "audio", "file": "file", "video": "video",
                "face": "face", "market_face": "face"}
PART_LABELS = {"image": "图片", "audio": "语音", "file": "文件", "video": "视频",
               "face": "表情", "unknown": "未知类型"}


def display_metadata(row):
    if row["status"] != "normal":
        return None
    parts = []
    def add(kind):
        if kind not in parts:
            parts.append(kind)
    quote = row["quote"]
    has_quote = isinstance(quote, str)
    raw = row["raw"]
    try:
        document = json.loads(raw) if isinstance(raw, str) and len(raw) <= MAX_RAW_CHARS else {}
    except (ValueError, RecursionError):
        document = {}
    if isinstance(document, dict):
        elements = document.get("elements")
        if isinstance(elements, list):
            for item in elements[:256]:
                if not isinstance(item, dict):
                    continue
                has_quote |= isinstance(item.get("replyElement"), dict)
                for field, kind in RAW_PARTS.items():
                    if isinstance(item.get(field), dict):
                        subtype = item[field].get("picSubType")
                        add("face" if field == "picElement" and type(subtype) is int and subtype == 1 else kind)
        content = document.get("content")
        if isinstance(content, dict):
            has_quote |= isinstance(content.get("reply"), dict)
            for field in ("elements", "resources", "emojis"):
                elements = content.get(field)
                if not isinstance(elements, list):
                    continue
                for item in elements[:256]:
                    if not isinstance(item, dict):
                        continue
                    kind = item.get("type")
                    has_quote |= kind == "reply"
                    if isinstance(kind, str) and kind in EXPORT_PARTS:
                        data = item.get("data")
                        sticker = kind == "image" and isinstance(data, dict) and data.get("subType") == "sticker"
                        add("face" if sticker else EXPORT_PARTS[kind])
    if not parts and row["kind"] in PART_LABELS and row["kind"] != "unknown":
        add(row["kind"])
    if row["kind"] == "unknown" and not parts and not has_quote:
        add("unknown")
    if not parts and not has_quote:
        return None
    return {"parts": parts, "hasQuote": bool(has_quote),
            "quoteText": quote[:MAX_QUOTE_CHARS] if isinstance(quote, str) else None,
            "quoteTruncated": isinstance(quote, str) and len(quote) > MAX_QUOTE_CHARS}


def preview(message):
    details = message.get("qqDisplay")
    if not details:
        return message["text"]
    if message["kind"] == "text" and message["text"]:
        return message["text"]
    return " ".join("[" + PART_LABELS[kind] + "]" for kind in details["parts"]) or "[引用消息]"

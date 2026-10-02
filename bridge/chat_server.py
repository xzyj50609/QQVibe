#!/usr/bin/env python3
"""Read-only local chat UI entry point; no WeChat database is opened on import."""
from __future__ import annotations

import re


def classify(message_type, content):
    type_name = (message_type if isinstance(message_type, str) else
                 str(message_type) if isinstance(message_type, int) else "消息")
    text = content if isinstance(content, str) else ""
    text = re.sub(r"^(?:wxid_[\w-]+|[\w-]+@chatroom)\s*:\s*", "", text.strip())
    if text.startswith(("<msg", "<?xml", "<sysmsg", "<revokemsg")):
        if "<revokemsg" in text:
            return "system", ""
        if "<sysmsg" in text:
            match = re.search(r"<(?:text|content)>(.*?)</(?:text|content)>", text, re.S)
            return "system", re.sub(r"<[^>]+>", "", match.group(1))[:80] if match else ""
        for tag, kind, label in (("<img", "image", "[图片]"), ("<emoji", "other", "[表情]"),
                                 ("<voicemsg", "other", "[语音]"), ("<videomsg", "other", "[视频]")):
            if tag in text:
                return kind, label
        if "<appmsg" in text:
            match = re.search(r"<title>(.*?)</title>", text, re.S)
            return "other", re.sub(r"<[^>]+>", "", match.group(1))[:80] if match else "[应用消息]"
        return "other", "[消息]"
    if type_name != "文本":
        if "图片" in type_name:
            return "image", "[图片]"
        return "other", "[" + type_name + "]"
    return "text", text


def main():
    # Embedded Python's isolated path omits the script directory. Import only
    # the bridge modules shipped beside this trusted entry point.
    import sys
    from pathlib import Path
    bridge_dir = str(Path(__file__).resolve().parent)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    from real_http import main as serve
    return serve(classify)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WeChat DB bridge sidecar using wechatauto-replica.

Speaks JSONL over stdin/stdout (UTF-8).
No external network, no sending messages, read-only.
Never writes raw chat text to stderr/logs.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional


def clean_message_text(msg: Dict[str, Any]) -> str:
    """Extract a displayable, clean text string for a message."""
    mtype = msg.get("type") or "文本"
    content = msg.get("content")
    if not isinstance(content, str):
        return f"[{mtype}]"
    
    content = content.strip()
    if mtype == "文本":
        # In group chats, content may prefix sender: "wxid_xxx:\nactual message"
        if ":\n" in content and (content.startswith("wxid_") or "@chatroom" in content):
            content = content.split(":\n", 1)[1].strip()
        return content if content else "[空文本]"
    elif mtype == "图片":
        return "[图片]"
    elif mtype in ("动画表情", "表情"):
        return "[动画表情]"
    elif mtype == "语音":
        return "[语音]"
    elif mtype == "视频":
        return "[视频]"
    elif "<title>" in content:
        match = re.search(r"<title>(.*?)</title>", content)
        if match and match.group(1).strip():
            title = match.group(1).strip()
            # If title is XML-escaped or CDATA
            if title.startswith("<![CDATA[") and title.endswith("]]>"):
                title = title[9:-3].strip()
            return f"[{title}]"
        return f"[{mtype}]"
    elif content.startswith("<?xml") or content.startswith("<msg"):
        match = re.search(r"<title>(.*?)</title>", content)
        if match and match.group(1).strip():
            title = match.group(1).strip()
            if title.startswith("<![CDATA[") and title.endswith("]]>"):
                title = title[9:-3].strip()
            return f"[{title}]"
        return f"[{mtype}]"
    
    return content if content else f"[{mtype}]"


class WeChatBridge:
    def __init__(self) -> None:
        self._db = None
        self._self_info: Optional[Dict[str, Any]] = None
        self._sender_index: Optional[Dict[int, str]] = None

    def _ensure_db(self):
        if self._db is None:
            from wechatauto import WeChatDB
            self._db = WeChatDB()
            try:
                self._self_info = self._db.get_self_info()
            except Exception:
                self._self_info = {}
            try:
                self._sender_index = self._db._sender_id_index()
            except Exception:
                self._sender_index = {}
        return self._db

    def get_self_info(self) -> Dict[str, Any]:
        self._ensure_db()
        return self._self_info or {}

    def get_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        db = self._ensure_db()
        raw_sessions = db.get_sessions(limit=limit) or []
        results = []
        for s in raw_sessions:
            username = s.get("username", "")
            if not username:
                continue
            try:
                nick = db.get_nickname(username) or username
            except Exception:
                nick = username
            last_time = s.get("last_time", 0)
            time_ms = int(last_time * 1000) if last_time else 0
            results.append({
                "username": username,
                "nickname": nick,
                "summary": s.get("summary", ""),
                "unread": s.get("unread", 0),
                "last_time": last_time,
                "time": time_ms,
                "last_sender": s.get("last_sender", ""),
            })
        return results

    def _determine_side(self, msg: Dict[str, Any], self_user: str, sender_index: Dict[int, str]) -> str:
        sender_id = msg.get("sender_id")
        sender_username = msg.get("sender_username", "")
        # WeChat convention: real_sender_id 2 or empty username with id 2 is self
        if sender_id in (2, "2"):
            return "self"
        if sender_username and self_user and sender_username == self_user:
            return "self"
        if sender_id and sender_index.get(sender_id) == self_user:
            return "self"
        return "other"

    def get_messages(self, user: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        db = self._ensure_db()
        self_user = (self._self_info or {}).get("username", "")
        sender_index = self._sender_index or {}
        raw_msgs = db.get_messages(user, limit=limit, offset=offset) or []
        
        results = []
        for m in raw_msgs:
            local_id = m.get("local_id", 0)
            side = self._determine_side(m, self_user, sender_index)
            create_time = m.get("create_time", 0)
            time_ms = int(create_time * 1000) if create_time else 0
            text = clean_message_text(m)
            msg_id = f"{user}_{local_id}"
            results.append({
                "id": msg_id,
                "local_id": local_id,
                "side": side,
                "text": text,
                "time": time_ms,
                "type": m.get("type", "文本"),
                "sort_seq": m.get("sort_seq", 0),
            })
        
        # db.get_messages returns descending sort_seq; reverse to chronological order (ascending)
        results.reverse()
        return results

    def get_latest(self, user: str, since_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        db = self._ensure_db()
        self_user = (self._self_info or {}).get("username", "")
        sender_index = self._sender_index or {}
        raw_msgs = db.get_new_messages(user, since_seq=since_seq, limit=limit) or []
        
        results = []
        for m in raw_msgs:
            local_id = m.get("local_id", 0)
            side = self._determine_side(m, self_user, sender_index)
            create_time = m.get("create_time", 0)
            time_ms = int(create_time * 1000) if create_time else 0
            text = clean_message_text(m)
            msg_id = f"{user}_{local_id}"
            results.append({
                "id": msg_id,
                "local_id": local_id,
                "side": side,
                "text": text,
                "time": time_ms,
                "type": m.get("type", "文本"),
                "sort_seq": m.get("sort_seq", 0),
            })
        return results


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    bridge = WeChatBridge()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            err_resp = {"id": "", "ok": False, "error": {"code": "bad_json", "message": str(e)}}
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        req_id = req.get("id", "")
        method = req.get("method", "")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        # Merge top-level params if present
        for key in ("user", "username", "limit", "offset", "sinceSeq", "since_seq"):
            if key in req and key not in params:
                params[key] = req[key]

        try:
            if method == "sessions":
                limit = int(params.get("limit", 100))
                sessions = bridge.get_sessions(limit=limit)
                resp = {"id": req_id, "ok": True, "result": sessions}
            elif method == "messages":
                user = params.get("user") or params.get("username")
                if not user:
                    raise ValueError("Missing 'user' parameter")
                limit = int(params.get("limit", 50))
                offset = int(params.get("offset", 0))
                msgs = bridge.get_messages(user=user, limit=limit, offset=offset)
                resp = {"id": req_id, "ok": True, "result": msgs}
            elif method == "latest":
                user = params.get("user") or params.get("username")
                if not user:
                    raise ValueError("Missing 'user' parameter")
                since_seq = int(params.get("sinceSeq", params.get("since_seq", 0)))
                limit = int(params.get("limit", 200))
                msgs = bridge.get_latest(user=user, since_seq=since_seq, limit=limit)
                resp = {"id": req_id, "ok": True, "result": msgs}
            elif method == "self_info":
                info = bridge.get_self_info()
                resp = {"id": req_id, "ok": True, "result": info}
            elif method == "close":
                resp = {"id": req_id, "ok": True, "result": "closed"}
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                break
            else:
                resp = {"id": req_id, "ok": False, "error": {"code": "unknown_method", "message": f"Unknown method: {method}"}}
        except Exception as ex:
            resp = {"id": req_id, "ok": False, "error": {"code": "bridge_error", "message": str(ex)}}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

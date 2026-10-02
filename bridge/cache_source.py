"""Cached-key initialization and reusable snapshots with upstream database/WAL decoding."""
from __future__ import annotations

import os
import re

from wechatauto.db import WeChatDB
from snapshot_cache import SnapshotCacheMixin


ANCHOR_DATABASES = frozenset(("session/session.db", "contact/contact.db"))
MESSAGE_DATABASE = re.compile(r"message/message_\d+\.db\Z")


def database_groups(rels):
    """Find role candidates inside the already selected account, plus message shards."""
    rels = tuple(rels)
    anchors = {rel for rel in rels if os.path.basename(rel.replace("\\", "/")).casefold()
               in ("session.db", "contact.db")}
    # Match the installed reader's case-sensitive message inventory exactly.
    messages = {rel for rel in rels if MESSAGE_DATABASE.fullmatch(rel.replace("\\", "/"))}
    return anchors, messages


def resolve_anchor_rels(rels, validated):
    """Prefer a verified canonical role; otherwise accept one verified fallback."""
    candidates, _messages = database_groups(rels)
    selected = {}
    for role in ("session", "contact"):
        valid = [rel for rel in candidates if os.path.basename(rel.replace("\\", "/")).casefold()
                 == role + ".db" and rel in validated]
        canonical = next((rel for rel in valid if rel.replace("\\", "/").casefold()
                          == role + "/" + role + ".db"), None)
        if canonical is not None:
            selected[role] = canonical
        elif len(valid) == 1:
            selected[role] = valid[0]
        else:
            return None
    return selected


class IncompleteKeyCache(RuntimeError):
    """A selected account's individually validated cache keys, without a usable reader."""

    def __init__(self, keys):
        super().__init__("local WeChat key cache unavailable for selected account")
        self.validated_keys = dict(keys)


class CacheOnlyWeChatDB(SnapshotCacheMixin, WeChatDB):
    def get_self_info(self):
        for rel, path, _ in self._db_files:
            if rel != self.anchor_rels["contact"]:
                continue
            conn = self._open(rel)
            try:
                row = conn.execute(
                    "SELECT username, nick_name, remark FROM contact WHERE username=? LIMIT 1",
                    (self.wxid,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                return {"username": row[0], "nick_name": row[1], "remark": row[2]}
        return {"username": self.wxid, "nick_name": "", "remark": ""}

    def _load_or_extract_keys(self, master_key=None):
        if master_key is not None:
            raise RuntimeError("cache-only mode does not accept a master key")
        self.master_key = None
        self.cfg_dword = None
        self._keys = {}
        stable = self._stable_key_file()
        cache_paths = [stable, self.keys_file, self.keys_file + ".bak"]
        files = {rel for rel, _, _ in self._db_files}
        _anchors, messages = database_groups(files)
        for path in cache_paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                candidates = self._load_key_cache(path)
            except (AttributeError, TypeError, ValueError):
                continue
            for rel in files - self._keys.keys():
                key = candidates.get(rel)
                if not isinstance(key, bytes) or len(key) not in (32, 48):
                    continue
                self._keys[rel] = key
                try:
                    valid = self._key_works(rel)
                except Exception:
                    valid = False
                if not valid:
                    del self._keys[rel]
        selected = resolve_anchor_rels(files, self._keys)
        if selected is None or not messages or (set(selected.values()) | messages) - self._keys.keys():
            validated = dict(self._keys)
            self._keys = {}
            raise IncompleteKeyCache(validated)
        self.anchor_rels = selected
        self.messages_ready = True

    def _auto_diagnose_key_failure(self, _rel):
        raise RuntimeError("local WeChat key cache unavailable for selected account")

    def extract_keys(self):
        raise RuntimeError("process key extraction disabled in cache-only mode")

    def extract_master_key(self):
        raise RuntimeError("process key extraction disabled in cache-only mode")

    def _all_key_candidates(self):
        raise RuntimeError("process key extraction disabled in cache-only mode")

"""Read-only WeChat adapter: account identity, snapshots, messages, and media.

No inference scheduling or analysis-cache writes belong in this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from backend_contracts import (
    AccountChangedError, AccountUnavailableError, MAX_IMAGE_BYTES, MAX_ISSUED_IMAGES,
    MessageWindow, MessageWindowBatch, MessagesUnavailableError,
    PROFILE_METADATA_CACHE_LIMIT, QUOTED_REPLY_TYPE, ROOT, SESSION_PREVIEWS, SYSTEM_NAMES,
)
from history_browser import encode_cursor


def active_account_dir():
    """Resolve a unique live account from WeChat's open-file metadata, never DB mtimes."""
    native_reader = str(ROOT / "native-reader")
    if native_reader not in sys.path:
        sys.path.insert(0, native_reader)
    from wr import discovery

    accounts = discovery.discover_account_dirs()
    roots = [(account, os.path.normcase(os.path.realpath(account.path))) for account in accounts]
    matches = set()
    for process in discovery.find_weixin_processes():
        for path in discovery.process_open_file_paths(process.pid):
            opened = os.path.normcase(os.path.realpath(path))
            for account, root in roots:
                if opened == root or opened.startswith(root + os.sep):
                    matches.add(account.id)
    match = next((account for account in accounts if account.id in matches), None)
    return Path(match.path).parent.resolve() if len(matches) == 1 and match else None


def positive_timestamp(value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (number * 1000 if number < 10_000_000_000 else number) if number > 0 else None


def session_preview(summary, message_type, sub_type=None):
    if isinstance(message_type, int):
        kind = message_type & 0xFFFFFFFF if message_type > 0xFFFF else message_type
        if kind == 49 and sub_type in (5, 6):
            return "[链接]" if sub_type == 5 else "[文件]"
        if kind in SESSION_PREVIEWS:
            return SESSION_PREVIEWS[kind]
    content = summary.strip() if isinstance(summary, str) else ""
    return "[消息]" if content.startswith("<") else content


def avatar_candidates(*urls):
    candidates = []
    for url in urls:
        if not isinstance(url, str) or not url or url != url.strip():
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (parsed.scheme.lower() in ("http", "https") and parsed.hostname and
                not parsed.username and not parsed.password and url not in candidates):
            candidates.append(url)
    return candidates


def contact_display(contacts, user):
    contact = contacts.get(user)
    if contact:
        return {**contact, "name": SYSTEM_NAMES.get(user, user) if contact["name"] == user else contact["name"]}
    return {"name": SYSTEM_NAMES.get(user, user), "avatar": "", "avatarCandidates": []}


def message_id(account, user, shard, row):
    identity = json.dumps([account, user, shard, row["local_id"], row["sort_seq"],
                           row["server_id"]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class WeChatSource:
    # Declared so the backend can attach the trusted source kind to an input record
    # instead of hard-coding it; a source that does not declare one is "unknown".
    kind = "wechat"

    def __init__(self, factory=None, classifier=None, media_factory=None, active_account_locator=None):
        self.factory = factory
        self.classifier = classifier
        self.media_factory = media_factory
        # Injected legacy factories remain fixed for synthetic tests. Production always resolves
        # the unique live account before accessing a cached reader.
        self.live_account = factory is None and active_account_locator is None
        self.dynamic_account = factory is None or active_account_locator is not None
        if self.live_account:
            from live_source import active_account_snapshot
            self.active_account_locator = active_account_snapshot
        else:
            self.active_account_locator = active_account_locator or active_account_dir
        self.lock = threading.RLock()
        self.closed = False
        self.db = None
        self._account_dir = None
        self._active_checked_at = 0.0
        self._request_context = threading.local()
        self._self_username = None
        self.issued_images = OrderedDict()
        self.window_images = {}
        self.profile_metadata_cache = OrderedDict()
        self.profile_overview_counts_cache = OrderedDict()
        self.media_reason = threading.local()

    def _release_db(self):
        self.db = None
        self._account_dir = None
        self._active_checked_at = 0.0
        self._self_username = None
        self.issued_images.clear()
        self.window_images.clear()
        self.profile_metadata_cache.clear()

    @contextmanager
    def request_scope(self):
        """Keep one reader for an HTTP request, including its final scope checks."""
        context = self._request_context
        previous = (getattr(context, "active", False), getattr(context, "reader", None))
        if not previous[0]:
            context.active, context.reader = True, None
        try:
            yield
        finally:
            context.active, context.reader = previous

    def _live_reader_valid(self, db, selection):
        from live_source import _pages, _readiness, _token, _valid_page_key
        from cache_source import resolve_anchor_rels

        pages = _pages(selection.account_dir)
        sessions_ready, messages_ready = _readiness(pages, getattr(db, "_keys", {}))
        token = _token(selection, pages, anchors_only=not db.messages_ready)
        validated = {rel for rel, raw in getattr(db, "_keys", {}).items()
                     if rel in pages and _valid_page_key(raw, pages[rel][2])}
        valid = (getattr(db, "_live_selection", None) == selection and
                 getattr(db, "_live_token", None) == token and
                 getattr(db, "anchor_rels", None) == resolve_anchor_rels(pages, validated) and
                 sessions_ready and (not db.messages_ready or messages_ready))
        return valid, token

    def require_messages_ready(self):
        if not self.live_account:
            return
        with self.lock:
            if not self._db(fresh=True).messages_ready:
                raise MessagesUnavailableError()

    def forget_account(self, account):
        """Drop only this account's open reader and volatile key references."""
        with self.lock:
            for key in list(self.profile_overview_counts_cache):
                if key[0] == account:
                    del self.profile_overview_counts_cache[key]
            if self.db is not None and str(self.db.account) == account:
                for name in ("_keys", "_volatile_keys"):
                    value = getattr(self.db, name, None)
                    if isinstance(value, dict):
                        value.clear()
                if hasattr(self.db, "master_key"):
                    self.db.master_key = None
                self._release_db()
            forget = getattr(self.factory, "forget_account", None)
            if callable(forget):
                forget(account)

    def close(self):
        """Stop this bridge instance from reopening any WeChat-derived snapshot."""
        with self.lock:
            self.closed = True
            if self.db is not None:
                self.forget_account(str(self.db.account))
            self._release_db()
            self.profile_overview_counts_cache.clear()

    def _db(self, fresh=False):
        with self.lock:
            if self.closed:
                raise AccountUnavailableError()
            if not self.dynamic_account and self.db is not None:
                return self.db
            if self.dynamic_account:
                if (self.db is not None and not self.live_account and not fresh and
                        time.monotonic() - self._active_checked_at < 0.5):
                    return self.db
                try:
                    selection = self.active_account_locator()
                    location = (selection.account_dir if self.live_account and selection is not None
                                else Path(selection).resolve() if selection is not None else None)
                except Exception as exc:
                    self._release_db()
                    raise AccountUnavailableError() from exc
                if location is None or not (location / "db_storage").is_dir():
                    self._release_db()
                    raise AccountUnavailableError()
                pinned = getattr(self._request_context, "reader", None)
                if self.live_account and pinned is not None:
                    try:
                        valid, _token_now = self._live_reader_valid(pinned, selection)
                    except Exception as exc:
                        raise AccountUnavailableError() from exc
                    if not valid or Path(pinned.account_dir).resolve() != location:
                        raise AccountUnavailableError()
                    return pinned
                if self.db is not None and self._account_dir != location:
                    self._release_db()
                if self.live_account and self.db is not None:
                    try:
                        valid, _token_now = self._live_reader_valid(self.db, selection)
                    except Exception:
                        valid = False
                    if not valid:
                        self._release_db()
                if self.factory is None:
                    if self.live_account:
                        from live_source import LiveWeChatFactory
                        self.factory = LiveWeChatFactory()
                    else:
                        from cache_source import CacheOnlyWeChatDB
                        self.factory = CacheOnlyWeChatDB
                if self.db is not None:
                    if self.live_account and not self.db.messages_ready:
                        try:
                            candidate = self.factory(db_dir=str(location.parent), account=location.name,
                                                     selection=selection)
                        except Exception:
                            candidate = None
                        if candidate is not None and candidate.messages_ready:
                            if self.active_account_locator() != selection:
                                raise AccountUnavailableError()
                            self._release_db()
                            self.db = candidate
                            self._account_dir = location
                            from live_source import _pages, _token
                            candidate._live_selection = selection
                            candidate._live_token = _token(selection, _pages(selection.account_dir),
                                                           anchors_only=not candidate.messages_ready)
                            valid, _current_token = self._live_reader_valid(candidate, selection)
                            if not valid:
                                self._release_db()
                                raise AccountUnavailableError()
                    self._active_checked_at = time.monotonic()
                    if self.live_account and getattr(self._request_context, "active", False):
                        self._request_context.reader = self.db
                    return self.db
            if self.factory is None:
                if self.live_account:
                    from live_source import LiveWeChatFactory
                    self.factory = LiveWeChatFactory()
                else:
                    from cache_source import CacheOnlyWeChatDB
                    self.factory = CacheOnlyWeChatDB
            try:
                if self.live_account:
                    db = self.factory(db_dir=str(location.parent), account=location.name,
                                      selection=selection)
                elif self.dynamic_account:
                    db = self.factory(db_dir=str(location.parent), account=location.name)
                else:
                    db = self.factory()
                if self.dynamic_account and (str(db.account) != location.name or
                                             Path(db.account_dir).resolve() != location):
                    raise AccountUnavailableError()
                if self.live_account and self.active_account_locator() != selection:
                    raise AccountUnavailableError()
                if self.live_account:
                    from live_source import _pages, _readiness, _token
                    pages = _pages(selection.account_dir)
                    sessions_ready, messages_ready = _readiness(pages, db._keys)
                    if not sessions_ready or db.messages_ready and not messages_ready:
                        raise AccountUnavailableError()
                    page_token = _token(selection, pages, anchors_only=not db.messages_ready)
            except Exception as exc:
                self._release_db()
                if self.dynamic_account:
                    raise AccountUnavailableError() from exc
                raise
            self.db = db
            self._account_dir = location if self.dynamic_account else None
            self._active_checked_at = time.monotonic()
            if self.live_account:
                db._live_selection = selection
                db._live_token = page_token
                if getattr(self._request_context, "active", False):
                    self._request_context.reader = db
            return db

    def identity(self):
        return self.verified_identity()

    def verified_identity(self, *, messages=False):
        with self.lock:
            db = self._db(fresh=True)
            if messages and self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            account = str(db.account)
            if not account or not getattr(db, "workdir", None):
                raise RuntimeError("WeChat account/workdir unavailable")
            workdir = Path(db.workdir).resolve()
            return account, workdir

    def self_user(self, db=None):
        with self.lock:
            standalone = db is None
            db = self._db(fresh=True) if standalone else db
            if self._self_username is None:
                self._self_username = db.get_self_info().get("username") or db.wxid
            if standalone and self._db(fresh=True) is not db:
                raise AccountChangedError()
            return self._self_username

    def _contacts(self, db):
        for rel, path, _ in db._db_files:
            if self.live_account and rel != db.anchor_rels["contact"]:
                continue
            if not self.live_account and Path(path).name != "contact.db":
                continue
            conn = db._open(rel)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(contact)")}
                fields = [field if field in columns else "''" for field in
                          ("username", "nick_name", "remark", "small_head_url", "big_head_url")]
                rows = conn.execute("SELECT " + ",".join(fields) + " FROM contact")
                contacts = {}
                for user, nick, remark, small, big in rows:
                    if user:
                        candidates = avatar_candidates(small, big)
                        name = (remark if remark and remark != user else
                                nick if nick and nick != user else SYSTEM_NAMES.get(user, user))
                        contacts[str(user)] = {"name": name,
                                               "avatar": candidates[0] if candidates else "",
                                               "avatarCandidates": candidates}
                return contacts
            finally:
                conn.close()
        raise RuntimeError("contact.db unavailable")

    def sessions(self):
        with self.lock:
            db = self._db(fresh=True)
            contacts = self._contacts(db)
            self_user = self.self_user(db)
            self_contact = contact_display(contacts, self_user)
            items = []
            for rel, path, _ in db._db_files:
                if self.live_account and rel != db.anchor_rels["session"]:
                    continue
                if not self.live_account and Path(path).name != "session.db":
                    continue
                conn = db._open(rel)
                try:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(SessionTable)")}
                    required = {"username", "unread_count", "summary", "last_timestamp", "last_msg_sender",
                                "last_sender_display_name", "sort_timestamp", "is_hidden"}
                    if not required <= columns:
                        raise RuntimeError("unsupported session table schema")
                    type_column = next((name for name in ("last_msg_type", "last_message_type") if name in columns), None)
                    subtype_column = "last_msg_sub_type" if "last_msg_sub_type" in columns else None
                    fields = ("username,unread_count,summary,last_timestamp,last_msg_sender,"
                              "last_sender_display_name,sort_timestamp," +
                              (type_column if type_column else "NULL") + "," +
                              (subtype_column if subtype_column else "NULL") + "," +
                              ("is_top" if "is_top" in columns else "NULL"))
                    rows = conn.execute("SELECT " + fields + " FROM SessionTable WHERE is_hidden=0 "
                                        "ORDER BY sort_timestamp DESC,rowid DESC")
                    while batch := rows.fetchmany(256):
                        for user, unread, summary, last_time, sender, sender_name, sort_time, message_type, sub_type, is_top in batch:
                            if not isinstance(user, str) or not user:
                                continue
                            contact = contact_display(contacts, user)
                            try:
                                unread_count = max(0, int(unread or 0))
                            except (TypeError, ValueError, OverflowError):
                                unread_count = 0
                            items.append({"username": user, **contact,
                                          "preview": session_preview(summary, message_type, sub_type),
                                          "time": positive_timestamp(last_time),
                                          "sortTimestamp": int(sort_time) if isinstance(sort_time, (int, float)) and sort_time > 0 else None,
                                          "unreadCount": unread_count, "lastMsgType": message_type,
                                          "lastMsgSubType": sub_type,
                                          "pinned": bool(is_top) if is_top in (0, 1) else None,
                                          "lastSender": sender_name or sender or "",
                                          "isGroup": user.endswith("@chatroom")})
                finally:
                    conn.close()
                break
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return {"self": {"username": self_user, **self_contact}, "sessions": items,
                    "account": str(db.account), "messagesReady": bool(getattr(db, "messages_ready", True))}

    def _shard_rows(self, db, user, limit, offset=0):
        found = db._msg_conns(user)
        rows = []
        try:
            for conn, table in found:
                if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                    raise RuntimeError("invalid message table")
                source_path = conn.execute("PRAGMA database_list").fetchone()[2]
                shard = Path(source_path).name
                if not shard.startswith("message__message_") or not shard.endswith(".db"):
                    raise RuntimeError("unidentified message shard")
                sender_index = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                records = conn.execute(
                    f"SELECT local_id,local_type,real_sender_id,create_time,message_content,compress_content,server_id,sort_seq FROM {table} "
                    "ORDER BY sort_seq DESC,local_id DESC LIMIT ?", (limit + offset,))
                for record in records:
                    rows.append((record, shard, sender_index))
        finally:
            for conn in {id(conn): conn for conn, _ in found}.values():
                conn.close()
        rows.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])), reverse=True)
        return rows[offset:offset + limit]

    @staticmethod
    def _quoted_reply_title(content):
        if not isinstance(content, str):
            return None
        start = content.find("<msg")
        if start > 0:
            content = content[start:]
        try:
            root = ElementTree.fromstring(content.strip())
        except ElementTree.ParseError:
            return None
        appmsg = root if root.tag == "appmsg" else root.find("./appmsg")
        if appmsg is None or appmsg.find("./refermsg") is None:
            return None
        subtype = appmsg.find("./type")
        if subtype is not None and (subtype.text or "").strip() != "57":
            return None
        title = appmsg.find("./title")
        text = "".join(title.itertext()).strip() if title is not None else ""
        return text if text and text not in ("[应用消息]", "[消息]", "[图片]", "[表情]") else None

    @staticmethod
    def _decoded_content(db, local_type, content, compressed):
        kind_name = db._msg_type_name(local_type)
        if isinstance(content, bytes):
            content = db._friendly_content(content, kind_name)
        if not content or content == f"[{kind_name}]":
            if isinstance(compressed, bytes) and compressed:
                content = db._friendly_content(compressed, kind_name)
        return kind_name, content

    def _classified_content(self, db, local_type, content, compressed):
        kind_name, content = self._decoded_content(db, local_type, content, compressed)
        quote = self._quoted_reply_title(content) if local_type == QUOTED_REPLY_TYPE else None
        kind, text = ("text", quote) if quote is not None else self.classifier(
            kind_name, content or f"[{kind_name}]")
        return kind_name, kind, text

    def _analyzable_counts(self, db, conn, table, sender_ids=None):
        """Count exactly the rendered text that portrait scanning can consume."""
        where = "local_type IN (?,?)"
        args = [1, QUOTED_REPLY_TYPE]
        if sender_ids is not None:
            if not sender_ids:
                return {}
            where += " AND real_sender_id IN (" + ",".join("?" for _ in sender_ids) + ")"
            args.extend(sender_ids)
        counts = {}
        for sender_id, local_type, content, compressed in conn.execute(
                f"SELECT real_sender_id,local_type,message_content,compress_content FROM {table} WHERE {where}", args):
            _name, kind, text = self._classified_content(db, local_type, content, compressed)
            if kind == "text" and isinstance(text, str) and text.strip():
                counts[sender_id] = counts.get(sender_id, 0) + 1
        return counts

    def _render_row(self, db, user, item, contacts, own_user):
        record, shard, senders = item
        local_id, local_type, sender_id, created, content, compressed, server_id, seq = record
        kind_name, kind, text = self._classified_content(db, local_type, content, compressed)
        if kind == "system":
            return None
        sender = senders.get(int(sender_id or 0), "")
        if not sender and not user.endswith("@chatroom"):
            sender = user
        contact = contact_display(contacts, sender)
        timestamp = int(created or 0)
        return {"id": message_id(str(db.account), user, shard,
                                 {"local_id": local_id, "sort_seq": seq, "server_id": server_id}),
                "historyCursor": encode_cursor(str(db.account), user, (int(seq), shard, int(local_id))),
                "side": "self" if sender and sender == own_user else "other", "text": text,
                "kind": "text" if kind == "text" else "image" if kind == "image" else "other",
                "time": timestamp * 1000 if timestamp < 10_000_000_000 else timestamp,
                "type": kind_name, "senderId": sender, "senderName": contact["name"],
                "senderAvatar": contact["avatar"], "senderAvatarCandidates": contact["avatarCandidates"],
                "_sort": [int(seq), shard, int(local_id)]}

    def messages(self, user, limit, offset=0):
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            result = []
            raw = self._shard_rows(db, user, limit + 1, offset)
            for item in reversed(raw[:limit]):
                message = self._render_row(db, user, item, contacts, own_user)
                if message:
                    result.append(message)
                    if message["kind"] == "image" and item[0][1] == 3:
                        key = (str(db.account), user, message["id"])
                        self.issued_images[key] = tuple(message["_sort"]) + (item[0][6],)
                        self.issued_images.move_to_end(key)
                        if len(self.issued_images) > MAX_ISSUED_IMAGES:
                            self.issued_images.popitem(last=False)
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return MessageWindow(result, len(raw) > limit)

    def message_windows(self, users, limit=80, expected_account=None):
        """Prepare first screens together, opening each existing snapshot shard once.

        This reads a bounded latest window per conversation, never its full history and
        never invokes inference. Account verification brackets the complete batch.
        """
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            if expected_account is not None and str(db.account) != expected_account:
                raise AccountChangedError()
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            tables = {user: "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest() for user in users}
            rows = {user: [] for user in users}
            for rel in db._message_dbs():
                conn = db._open(rel)
                try:
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    found = [(user, table) for user, table in tables.items() if table in available]
                    if not found:
                        continue
                    senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    for user, table in found:
                        records = conn.execute(
                            f"SELECT local_id,local_type,real_sender_id,create_time,message_content,"
                            f"compress_content,server_id,sort_seq FROM {table} "
                            "ORDER BY sort_seq DESC,local_id DESC LIMIT ?", (limit + 1,))
                        rows[user].extend((record, shard, senders) for record in records)
                finally:
                    conn.close()
            windows, images, has_more_before = {}, {}, {}
            for user, records in rows.items():
                records.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])), reverse=True)
                has_more_before[user] = len(records) > limit
                windows[user], images[user] = [], {}
                for item in reversed(records[:limit]):
                    message = self._render_row(db, user, item, contacts, own_user)
                    if message:
                        windows[user].append(message)
                        if message["kind"] == "image" and item[0][1] == 3:
                            images[user][message["id"]] = tuple(message["_sort"]) + (item[0][6],)
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            # Each prepared conversation retains only the image capabilities belonging to
            # its current window, so preloading later chats cannot evict earlier images.
            for user in users:
                self.window_images[(str(db.account), user)] = images[user]
            return MessageWindowBatch(windows, has_more_before)

    def texts_for_refs(self, user, refs, with_ids=False):
        """Read only analyzed message rows by their stored shard and local primary key."""
        self.require_messages_ready()
        if not refs:
            return []
        with self.lock:
            db = self._db(fresh=True)
            by_shard = {}
            for shard, local_id, stable_id in refs:
                by_shard.setdefault(shard, {}).setdefault(int(local_id), set()).add(stable_id)
            table = "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest()
            texts = []
            for rel, _path, _size in db._db_files:
                shard = rel.replace(os.sep, "__")
                selected = by_shard.get(shard)
                if not selected:
                    continue
                conn = db._open(rel)
                try:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
                    ).fetchone()
                    if not exists:
                        continue
                    ids = list(selected)
                    for start in range(0, len(ids), 400):
                        batch = ids[start:start + 400]
                        placeholders = ",".join("?" for _ in batch)
                        rows = conn.execute(
                            f"SELECT local_id,local_type,message_content,compress_content,server_id,sort_seq "
                            f"FROM {table} WHERE local_id IN ({placeholders})", batch
                        )
                        for local_id, local_type, content, compressed, server_id, sort_seq in rows:
                            stable_id = message_id(str(db.account), user, shard,
                                                   {"local_id": local_id, "sort_seq": sort_seq,
                                                    "server_id": server_id})
                            if stable_id not in selected.get(int(local_id), ()):
                                continue
                            kind_name = db._msg_type_name(local_type)
                            if isinstance(content, bytes):
                                content = db._friendly_content(content, kind_name)
                            placeholder = f"[{kind_name}]"
                            if (not content or content == placeholder) and isinstance(compressed, bytes):
                                content = db._friendly_content(compressed, kind_name)
                            kind, text = self.classifier(kind_name, content or placeholder)
                            if kind == "text" and isinstance(text, str) and text.strip():
                                texts.append((stable_id, text) if with_ids else text)
                finally:
                    conn.close()
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return texts

    def media(self, user, stable_id):
        self.require_messages_ready()
        self.media_reason.value = None
        def unavailable(reason):
            self.media_reason.value = reason
            return None
        if not isinstance(stable_id, str) or not re.fullmatch(r"[0-9a-f]{64}", stable_id):
            return unavailable("invalid-id")
        with self.lock:
            db = self._db(fresh=True)
            account = str(db.account)
            issued = (self.issued_images.get((account, user, stable_id)) or
                      self.window_images.get((account, user), {}).get(stable_id))
            if issued is None:
                return unavailable("not-issued")
            seq, shard, local_id, server_id = issued
            if stable_id != message_id(account, user, shard,
                                       {"local_id": local_id, "sort_seq": seq, "server_id": server_id}):
                return unavailable("identity-mismatch")
            found = db._msg_conns(user)
            row = None
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        continue
                    path = conn.execute("PRAGMA database_list").fetchone()[2]
                    if Path(path).name != shard:
                        continue
                    matches = conn.execute(
                        f"SELECT local_type,create_time,message_content,packed_info_data FROM {table} "
                        "WHERE local_id=? AND sort_seq=? AND server_id=? LIMIT 2",
                        (local_id, seq, server_id),
                    ).fetchall()
                    if len(matches) == 1 and matches[0][0] == 3:
                        _, created, content, packed = matches[0]
                        row = {"local_type": 3, "create_time": created,
                               "content": content, "packed_info": packed}
                    break
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
        if row is None:
            return unavailable("row-unavailable")
        try:
            if self.media_factory is None:
                from wechatauto.media import MediaDownloader
                downloader = MediaDownloader(db)
            else:
                downloader = self.media_factory(db)
            md5 = downloader._img_md5(row)
            if not md5 or not re.fullmatch(r"[0-9a-f]{32}", md5):
                return unavailable("metadata-unavailable")
            dat_path = (downloader._find_dat(user, md5, row["create_time"]) or
                        downloader._find_dat(user, md5, row["create_time"], thumbnail=True))
            base = (Path(db.account_dir) / "msg" / "attach" /
                    hashlib.md5(user.encode("utf-8")).hexdigest()).resolve()
            if (not dat_path or Path(dat_path).name not in (md5 + ".dat", md5 + "_t.dat") or
                    not Path(dat_path).resolve().is_relative_to(base)):
                return unavailable("local-file-unavailable")
            if Path(dat_path).stat().st_size > MAX_IMAGE_BYTES:
                return unavailable("image-too-large")
            with open(dat_path, "rb") as file:
                magic = file.read(6)
            aes_key = xor_key = None
            if magic == b"\x07\x08\x56\x32\x08\x07":
                derived = downloader._derive_cfg_key()
                if derived:
                    aes_key, xor_key = derived
                else:
                    aes_key = downloader._load_persisted_key()
                    if not aes_key:
                        return unavailable("local-key-unavailable")
                    xor_key = downloader._derive_xor_key(dat_path)
            data = downloader.decrypt_image(dat_path, aes_key=aes_key, xor_key=xor_key)
        except (OSError, ValueError, RuntimeError):
            return unavailable("decode-failed")
        if not isinstance(data, bytes) or len(data) > MAX_IMAGE_BYTES:
            return unavailable("image-too-large")
        if data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            return unavailable("unsupported-format")
        with self.lock:
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
        return data, mime

    def history_highwater(self, user):
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            newest = self._shard_rows(db, user, 1)
            if not newest:
                return None
            record, shard, _ = newest[0]
            return int(record[7]), shard, int(record[0])

    def history_page(self, user, highwater, after=None, page_size=256):
        with self.lock:
            # Resolve and validate one reader per page. Re-discovering the same
            # live account for readiness, reader access and self_user made every
            # page pay several Windows ownership queries before reading any rows.
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            if highwater is None:
                return [], None
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            found = db._msg_conns(user)
            rows = []
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    clauses, params = [], []
                    high_seq, high_shard, high_local = highwater
                    if shard == high_shard:
                        clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id <= ?))")
                        params.extend((high_seq, high_seq, high_local))
                    else:
                        clauses.append("sort_seq <= ?" if shard < high_shard else "sort_seq < ?")
                        params.append(high_seq)
                    if after is not None:
                        after_seq, after_shard, after_local = after
                        if shard == after_shard:
                            clauses.append("(sort_seq > ? OR (sort_seq = ? AND local_id > ?))")
                            params.extend((after_seq, after_seq, after_local))
                        else:
                            clauses.append("sort_seq >= ?" if shard > after_shard else "sort_seq > ?")
                            params.append(after_seq)
                    sql = (f"SELECT local_id,local_type,real_sender_id,create_time,message_content,compress_content,server_id,sort_seq "
                           f"FROM {table} WHERE {' AND '.join(clauses)} ORDER BY sort_seq ASC,local_id ASC LIMIT ?")
                    senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    rows.extend((record, shard, senders) for record in conn.execute(sql, (*params, page_size)))
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            rows.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])))
            page = rows[:page_size]
            if not page:
                return [], None
            last_record, last_shard, _ = page[-1]
            next_after = (int(last_record[7]), last_shard, int(last_record[0]))
            messages = [self._render_row(db, user, item, contacts, own_user) for item in page]
            return [message for message in messages if message], next_after

    def quoted_history_page(self, user, ceiling, after=None, page_size=64, member=None):
        """Read only 49/57 candidates inside an already-consumed history prefix."""
        self.require_messages_ready()
        if ceiling is None:
            return [], None
        with self.lock:
            db = self._db()
            contacts = self._contacts(db)
            own_user = self.self_user()
            found = db._msg_conns(user)
            rows = []
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    clauses, params = ["local_type=?"], [QUOTED_REPLY_TYPE]
                    if member is not None:
                        selected_ids = [row[0] for row in conn.execute(
                            "SELECT rowid FROM Name2Id WHERE user_name=?", (member,))]
                        if not selected_ids:
                            continue
                        clauses.append("real_sender_id IN (" + ",".join("?" for _ in selected_ids) + ")")
                        params.extend(selected_ids)
                    high_seq, high_shard, high_local = ceiling
                    if shard == high_shard:
                        clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id <= ?))")
                        params.extend((high_seq, high_seq, high_local))
                    else:
                        clauses.append("sort_seq <= ?" if shard < high_shard else "sort_seq < ?")
                        params.append(high_seq)
                    if after is not None:
                        after_seq, after_shard, after_local = after
                        if shard == after_shard:
                            clauses.append("(sort_seq > ? OR (sort_seq = ? AND local_id > ?))")
                            params.extend((after_seq, after_seq, after_local))
                        else:
                            clauses.append("sort_seq >= ?" if shard > after_shard else "sort_seq > ?")
                            params.append(after_seq)
                    sql = ("SELECT local_id,local_type,real_sender_id,create_time,message_content,"
                           "compress_content,server_id,sort_seq "
                           f"FROM {table} WHERE {' AND '.join(clauses)} "
                           "ORDER BY sort_seq ASC,local_id ASC LIMIT ?")
                    senders = {int(row[0]): row[1] for row in conn.execute(
                        "SELECT rowid,user_name FROM Name2Id")}
                    rows.extend((record, shard, senders) for record in conn.execute(
                        sql, (*params, page_size)))
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            rows.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])))
            page = rows[:page_size]
            if not page:
                return [], None
            last, shard, _senders = page[-1]
            next_after = (int(last[7]), shard, int(last[0]))
            rendered = [self._render_row(db, user, item, contacts, own_user) for item in page]
            return [item for item in rendered if item], next_after

    def preceding_text_context(self, user, before, limit=3):
        """Fetch the nearest earlier text context without replaying older history."""
        self.require_messages_ready()
        with self.lock:
            db = self._db()
            contacts = self._contacts(db)
            own_user = self.self_user()
            found = db._msg_conns(user)
            candidates = []
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    cursor = None
                    senders = {int(row[0]): row[1] for row in conn.execute(
                        "SELECT rowid,user_name FROM Name2Id")}
                    found_here = 0
                    while found_here < limit:
                        clauses = ["local_type IN (?,?)"]
                        params = [1, QUOTED_REPLY_TYPE]
                        if cursor is None:
                            seq, target_shard, local = before
                            if shard == target_shard:
                                clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id < ?))")
                                params.extend((seq, seq, local))
                            else:
                                clauses.append("sort_seq <= ?" if shard < target_shard else "sort_seq < ?")
                                params.append(seq)
                        else:
                            seq, local = cursor
                            clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id < ?))")
                            params.extend((seq, seq, local))
                        sql = ("SELECT local_id,local_type,real_sender_id,create_time,message_content,"
                               "compress_content,server_id,sort_seq "
                               f"FROM {table} WHERE {' AND '.join(clauses)} "
                               "ORDER BY sort_seq DESC,local_id DESC LIMIT ?")
                        rows = conn.execute(sql, (*params, 16)).fetchall()
                        if not rows:
                            break
                        for record in rows:
                            item = self._render_row(db, user, (record, shard, senders),
                                                    contacts, own_user)
                            if item and item["kind"] == "text" and item["text"].strip():
                                candidates.append(item)
                                found_here += 1
                                if found_here >= limit:
                                    break
                        cursor = int(rows[-1][7]), int(rows[-1][0])
                        if len(rows) < 16:
                            break
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            newest = sorted(candidates, key=lambda item: tuple(item["_sort"]), reverse=True)[:limit]
            return [{"id": item["id"], "side": item["side"], "text": item["text"]}
                    for item in reversed(newest)]

    def stats(self, user, member=None):
        self.require_messages_ready()
        with self.lock:
            db = self._db()
            contacts = self._contacts(db)
            found = db._msg_conns(user)
            count = text_count = 0
            members = set()
            try:
                for conn, table in found:
                    sender_index = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    selected_ids = [sender_id for sender_id, username in sender_index.items() if username == member] if member else []
                    if member and not selected_ids:
                        amount = 0
                    elif member:
                        placeholders = ",".join("?" for _ in selected_ids)
                        amount = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE real_sender_id IN ({placeholders})", selected_ids).fetchone()[0]
                    else:
                        amount = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    count += amount
                    text_count += sum(self._analyzable_counts(
                        db, conn, table, selected_ids if member else None).values())
                    for (sender_id,) in conn.execute(f"SELECT DISTINCT real_sender_id FROM {table}"):
                        sender = sender_index.get(int(sender_id or 0))
                        if sender:
                            members.add(sender)
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            return count, text_count, [{"id": member, **contact_display(contacts, member)}
                                       for member in sorted(members)]

    def profile_metadata(self, user, member=None):
        """Read metadata once per snapshot revision; Backend brackets the account scope."""
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            found = db._msg_conns(user)
            try:
                signatures = []
                for conn, _table in found:
                    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                    stat = path.stat()
                    signatures.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
                signature = tuple(signatures)
                key = (str(db.account), user)
                cached = self.profile_metadata_cache.get(key)
                if cached and cached[0] == signature:
                    counts, count, text_count = cached[1:]
                    self.profile_metadata_cache.move_to_end(key)
                else:
                    counts, count, text_count = {}, 0, 0
                    for conn, table in found:
                        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                            raise RuntimeError("invalid message table")
                        senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                        for sender_id, amount in conn.execute(
                                f"SELECT real_sender_id,COUNT(*) FROM {table} GROUP BY real_sender_id"):
                            count += amount
                            sender = senders.get(int(sender_id or 0))
                            if sender:
                                previous = counts.get(sender, (0, 0))
                                counts[sender] = (previous[0] + amount, previous[1])
                        for sender_id, analyzed in self._analyzable_counts(db, conn, table).items():
                            text_count += analyzed
                            sender = senders.get(int(sender_id or 0))
                            if sender:
                                previous = counts.get(sender, (0, 0))
                                counts[sender] = (previous[0], previous[1] + analyzed)
                    self.profile_metadata_cache[key] = (signature, counts, count, text_count)
                    self.profile_metadata_cache.move_to_end(key)
                    if len(self.profile_metadata_cache) > PROFILE_METADATA_CACHE_LIMIT:
                        self.profile_metadata_cache.popitem(last=False)
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            group = user.endswith("@chatroom")
            if member and (not group or member not in counts):
                raise ValueError("unknown member")
            subject = member if group else user
            if subject:
                count, text_count = counts.get(subject, (0, 0))
            return {"contact": contact_display(contacts, member or user),
                    "members": [{"id": sender, **contact_display(contacts, sender)} for sender in sorted(counts)] if group else [],
                    "count": count, "textCount": text_count}

    def profile_overview(self, user, member=None, highwater=None):
        """Contact, member list, and per-sender message counts without classifying text.

        The API portrait read path must never re-render the analysis of every message
        just to show who is in a group: text totals come from the persisted portrait
        inventory/checkpoint instead.
        """
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            # A live reader may be replaced while the account and newest message
            # stay the same. Reuse the counts across that replacement, with a
            # bounded refresh window for older-message backfills.
            # The same account name can exist under different local WeChat roots.
            account_dir = getattr(db, "account_dir", None) or getattr(db, "workdir", "")
            account_dir = os.path.normcase(str(Path(account_dir).resolve())) if account_dir else ""
            cache_key = (str(db.account), account_dir, user, highwater)
            cached = self.profile_overview_counts_cache.get(cache_key)
            if cached is not None and cached[2] > time.monotonic():
                counts, total, _expires = cached
                self.profile_overview_counts_cache.move_to_end(cache_key)
            else:
                found = db._msg_conns(user)
                counts = {}
                total = 0
                try:
                    for conn, table in found:
                        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                            raise RuntimeError("invalid message table")
                        senders = {int(row[0]): row[1] for row in
                                   conn.execute("SELECT rowid,user_name FROM Name2Id")}
                        for sender_id, amount in conn.execute(
                                f"SELECT real_sender_id,COUNT(*) FROM {table} GROUP BY real_sender_id"):
                            total += amount
                            sender = senders.get(int(sender_id or 0))
                            if sender:
                                counts[sender] = counts.get(sender, 0) + amount
                finally:
                    for conn in {id(conn): conn for conn, _ in found}.values():
                        conn.close()
                self.profile_overview_counts_cache[cache_key] = (counts, total, time.monotonic() + 60)
                while len(self.profile_overview_counts_cache) > PROFILE_METADATA_CACHE_LIMIT:
                    self.profile_overview_counts_cache.popitem(last=False)
            group = user.endswith("@chatroom")
            if member and (not group or member not in counts):
                raise ValueError("unknown member")
            return {"contact": contact_display(contacts, member or user),
                    "members": [{"id": sender, **contact_display(contacts, sender)}
                                for sender in sorted(counts)] if group else [],
                    "count": counts.get(member, 0) if member else
                             (total if group else counts.get(user, 0)),
                    "textCount": None}

    def invalidate_profile_metadata(self, user=None):
        """Public invalidation entry (source contract §2.3); callers never touch the cache."""
        with self.lock:
            if user is None:
                self.profile_metadata_cache.clear()
                return
            for key in [key for key in self.profile_metadata_cache if key[1] == user]:
                del self.profile_metadata_cache[key]

    def contact(self, user):
        with self.lock:
            db = self._db(fresh=True)
            contact = contact_display(self._contacts(db), user)
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return contact

"""Offline QQ source (migration contract T03-B.3, interface local-core-v1).

Implements the duck-typed surface the backend already consumes, reading only the
product's own message library. No QCE request, token or WeChat scan happens here:
the real connector is T06 and is still gated on the live measurements.

Two things deliberately differ from WeChatSource (source contract 2.5): identity is
derived, never read from a WeChat table, and callers reach the profile cache through
``invalidate_profile_metadata()`` only.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from backend_contracts import (AccountChangedError, AccountUnavailableError,
                              MessagesUnavailableError, MessageWindow, MessageWindowBatch)
from qq_identity import account_key, canonical_uin
from qq_entities import qq_avatar
from analysis_targets import SELF_SUBJECT
from product_profile import ROOT, current_product
from qq_message_store import QQMessageStore, _check_scope, open_store

CURSOR_VERSION = 2
PLATFORM = "qq"
# First release has no media reader at all: report why instead of probing WeChat.
MEDIA_REASON = "qq-media-not-implemented"
UNKNOWN_KIND_LABEL = "[未知类型]"


def guarded_read(method):
    """Hold the source binding stable for one read, not across model/network work."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.lock, self.request_scope():
            return method(self, *args, **kwargs)
    return guarded


def encode_cursor(account, conversation_key, position, revision=1):
    """An opaque cursor that carries its own scope (source contract 4.3)."""
    time_ms, _scope, local_seq = _check_scope(position, conversation_key)
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid history revision")
    raw = json.dumps([CURSOR_VERSION, PLATFORM, account, conversation_key, int(time_ms),
                      int(local_seq), revision], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(value, account, conversation_key, revision=None):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value):
        raise ValueError("invalid history cursor")
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid history cursor")
    if (not isinstance(payload, list) or len(payload) != 7 or type(payload[0]) is not int or payload[0] != CURSOR_VERSION or
            payload[1] != PLATFORM):
        raise ValueError("unsupported cursor version or platform")
    if payload[2] != account or payload[3] != conversation_key:
        # Never borrow another account's or conversation's position (source contract 4.2).
        raise ValueError("invalid cursor scope")
    if type(payload[6]) is not int or payload[6] < 1:
        raise ValueError("invalid history revision")
    if revision is not None and payload[6] != revision:
        raise ValueError("stale-history-cursor")
    return tuple(_check_scope((payload[4], "qq:" + conversation_key, payload[5]), conversation_key))


def render_message(account, row, revision=1):
    """QQ's shared display projection. Withheld text can never become model input."""
    if row["direction"] in ("system", "conflict"):
        return None
    kind = row["kind"] if row["kind"] in ("text", "image") else "other"
    text = row["text"] or (UNKNOWN_KIND_LABEL if row["kind"] == "unknown" else "")
    if row["status"] in ("recalled", "conflict"):
        kind = "other"
        text = "[消息已撤回]" if row["status"] == "recalled" else "[消息存在冲突]"
    position = (row["time_ms"], "qq:" + row["conversation_key"], row["local_seq"])
    rendered = {"id": row["native_id"], "historyCursor": encode_cursor(account, row["conversation_key"], position, revision),
            "side": "self" if row["direction"] == "self" else "other", "text": text,
            "kind": kind, "time": row["time_ms"], "type": str(row["msg_type"]),
            "senderId": row["sender_uid"] or row["sender_uin"] or "", "senderName": "",
            "senderAvatar": "", "senderAvatarCandidates": [], "_sort": position}
    from qq_message_display import display_metadata
    details = display_metadata(row)
    if details is not None:
        rendered["qqDisplay"] = details
    return rendered


class QQSource:
    kind = "qq"

    def __init__(self, *, uin=None, store=None, profile=None, root=None):
        self.lock = threading.RLock()
        self._uin = canonical_uin(uin) if uin is not None else None
        self._account = account_key(self._uin) if self._uin else None
        self._store = store
        self._profile = profile or current_product()
        if store is not None and self._account is None:
            raise ValueError("a message library requires an account")
        if root is None and store is not None:
            # Injected libraries must never make a later attach escape to the checkout.
            parent = store.path.parent
            root = (parent.parents[2] if parent.name == self._account[2:] and
                    parent.parent.name == "accounts" and
                    parent.parent.parent.name == self._profile.data_dir else parent)
        self._root = Path(root if root is not None else ROOT).resolve()
        self._generation = 0
        self._closed = False
        self._scope = threading.local()
        # Production consumers read ``source.media_reason.value`` (real_http.py), so this
        # stays the same shape as WeChatSource instead of becoming a method.
        self.media_reason = threading.local()
        self.media_reason.value = MEDIA_REASON
        if store is not None:
            store.assert_account(self._account)

    # -- readiness ---------------------------------------------------------------

    def _library(self):
        if self._closed or self._store is None or self._store.closed:
            raise MessagesUnavailableError()
        return self._store

    @guarded_read
    def identity(self):
        return self.verified_identity()

    def binding_token(self):
        """Detect A→B→A as well as simple switches across asynchronous source work."""
        with self.lock:
            return self._account, self._generation, self._closed

    @guarded_read
    def verified_identity(self, *, messages=False):
        if self._closed or self._account is None:
            raise AccountUnavailableError()
        if messages:
            self.require_messages_ready()
        directory = (self._store.path.parent if self._store is not None else
                     self._profile.account_directory(self._account, self._root))
        return self._account, directory

    @guarded_read
    def require_messages_ready(self):
        # Account-level readiness only; an empty but synced conversation is a legal
        # empty state, and being offline does not hide local history (F17).
        self._library()

    @contextmanager
    def request_scope(self):
        """A request must not silently mix two account generations."""
        with self.lock:
            observed = (self._account, self._generation, self._store)
        try:
            yield
        finally:
            with self.lock:
                if observed != (self._account, self._generation, self._store):
                    raise AccountChangedError()

    @contextmanager
    def read_library(self, expected_account):
        """Public local-history read boundary; no WeChat snapshot or private adapter API."""
        with self.lock, self.request_scope():
            if expected_account != self._account:
                raise AccountChangedError()
            store = self._library()
            with store.lock:
                yield self._account, store

    # -- directory ---------------------------------------------------------------

    def data_scope(self, account, user):
        from qq_support import local_scope
        with self.read_library(account) as (_, store):
            return local_scope(store, account, user)

    def ingest_history(self, account, user, *, before=None, limit=20, message_cursor=None, kind=None):
        from qq_ingest_audit import page
        from qq_support import read_snapshot
        with self.read_library(account) as (_, store), read_snapshot(store.connection):
            state = store.revision(account, user)
            if state is None:
                raise ValueError("unknown-local-conversation")
            key = None
            if message_cursor is not None:
                position = decode_cursor(message_cursor, account, user, state[0])
                row = store.connection.execute("SELECT message_key FROM messages WHERE account_key=? AND conversation_key=? "
                    "AND time_ms=? AND local_seq=?", (account, user, position[0], position[2])).fetchone()
                if row is None:
                    raise ValueError("unknown-local-message")
                key = row[0]
            return {"account": account, "user": user, "dataRevision": state[0],
                    **page(store.connection, account, user, before, limit, key, kind)}

    def _contact(self, row):
        name = row["display_name"] if row is not None else None
        profile = self._library().conversation_profile(self._account,row['conversation_key']) if row is not None else None
        if profile and (row['kind']=='group' or not name or name == row['peer_uin'] or name == row['group_code']):
            name = profile['name'] or name
        image = profile['avatar_url'] if profile else ''
        if not image and row is not None:
            number = row['group_code'] if row['kind']=='group' else row['peer_uin']
            if number:
                image = qq_avatar(number,group=row['kind']=='group')
        return {"name": name or (row["conversation_key"] if row is not None else ""),
                "avatar": image, "avatarCandidates": [image] if image else []}

    @guarded_read
    def conversation_kind(self,user):
        row=self._conversation(user)
        if row is None:
            raise ValueError('conversation-not-found')
        return row['kind']

    @guarded_read
    def members(self,user):
        rows=self._library().members(self._account,user)
        counts=self._library().sender_counts(self._account,user)
        def count(uin,uid):
            return (counts.get('uin:'+uin,0) if uin else 0)+(counts.get('uid:'+uid,0) if uid else 0)
        result=[{'username':row['member_id'],'id':row['member_id'],
                 'name':row['card_name'] or row['nickname'] or row['uin'] or '成员',
                 'displayName':row['card_name'] or row['nickname'] or row['uin'] or '成员',
                 'avatar':row['avatar_url'],'avatarCandidates':[row['avatar_url']] if row['avatar_url'] else [],
                 'isSelf':row['uin']==self._uin,'active':bool(row['active']),
                 'uin':row['uin'],'uid':row['uid'],
                 'membershipState':'observed' if row['source']!='qce' else 'current' if row['active'] else 'left',
                 'count':count(row['uin'],row['uid'])} for row in rows]
        if self.conversation_kind(user)=='group' and not any(row['isSelf'] for row in result):
            person=self._library().person_for_sender(self._account,uin=self._uin)
            own_id=person['member_id'] if person else 'uin:'+self._uin
            result.append({'username':own_id,'id':own_id,**self._self_contact(),'displayName':self._self_contact()['name'],
                'isSelf':True,'active':False,'uin':self._uin,'uid':person['uid'] if person else None,
                'count':count(self._uin,person['uid'] if person else None),
                'membershipState':'account-target'})
        return result

    @guarded_read
    def contact(self, user):
        return self._contact(self._conversation(user))

    def _conversation(self, conversation_key):
        store = self._library()
        account = self._account
        with store.lock:
            return store.connection.execute(
                "SELECT * FROM conversations WHERE account_key=? AND conversation_key=?",
                (account, conversation_key)).fetchone()

    @guarded_read
    def sessions(self):
        from qq_message_display import preview as message_preview
        self.verified_identity()
        store = self._library()
        account = self._account
        items = []
        for row in store.list_conversations(account):
            latest = store.latest(account, row["conversation_key"], 1)[0]
            preview = self._row(latest[0], row["data_revision"]) if latest else None
            display = self._contact(row)
            items.append({"username": row["conversation_key"], "displayName": display["name"],
                          "name": display["name"], "avatar": display['avatar'], "avatarCandidates": display['avatarCandidates'],
                          "preview": message_preview(preview) if preview else "",
                          "time": latest[0]["time_ms"] if latest else 0,
                          "sortTimestamp": latest[0]["time_ms"] if latest else 0,
                          "unreadCount": 0, "lastMsgType": 0, "lastMsgSubType": 0,
                          "pinned": bool(row["selected"]), "lastSender": "",
                          "isGroup":row['kind']=='group'})
            if row['kind']=='group':
                items[-1]['conversationKind']='group'
        person=store.person_for_sender(account,uin=self._uin)
        me={'name':person['nickname'] or self._uin,'avatar':person['avatar_url'],
            'avatarCandidates':[person['avatar_url']] if person['avatar_url'] else []} if person else {
                'name':self._uin,'avatar':qq_avatar(self._uin),
                'avatarCandidates':[qq_avatar(self._uin)]}
        return {"self": {"username": self._uin, **me}, "sessions": items,
                "account": account, "messagesReady": True}

    def _self_contact(self):
        person=self._library().person_for_sender(self._account,uin=self._uin)
        image=person['avatar_url'] if person and person['avatar_url'] else qq_avatar(self._uin)
        return {'name':person['nickname'] if person and person['nickname'] else self._uin,
                'avatar':image,'avatarCandidates':[image]}

    # -- message rows ------------------------------------------------------------

    def _row(self, row, revision=1):
        rendered=render_message(self._account, row, revision)
        group=self._conversation(row['conversation_key'])['kind']=='group'
        if rendered is None and group:
            position=(row['time_ms'],'qq:'+row['conversation_key'],row['local_seq'])
            rendered={'id':row['native_id'],'historyCursor':encode_cursor(self._account,row['conversation_key'],position,revision),
                'side':'other','text':'[系统消息]' if row['direction']=='system' else '[发言者未知或身份冲突]',
                'kind':'system' if row['direction']=='system' else 'unknown','time':row['time_ms'],'type':str(row['msg_type']),
                'senderId':'','senderName':'系统' if row['direction']=='system' else '未知发言者',
                'senderAvatar':'','senderAvatarCandidates':[],'_sort':position}
        if rendered is not None:
            person=self._library().person_for_sender(self._account,uin=row['sender_uin'],uid=row['sender_uid'])
            if person:
                member=self._library().membership(self._account,row['conversation_key'],person['member_id'])
                rendered.update(senderName=(member['card_name'] if member else '') or person['nickname'] or person['uin'] or '成员',
                    senderAvatar=person['avatar_url'],senderAvatarCandidates=[person['avatar_url']] if person['avatar_url'] else [])
                if group:
                    rendered['senderId']=person['member_id']
            if group:
                rendered['conversationKind']='group'
                from qq_roles import projection
                def resolve(uin,uid):
                    from qq_entities import UID
                    try:uin=canonical_uin(uin)
                    except ValueError:uin=None
                    uid=uid if isinstance(uid,str) and UID.fullmatch(uid) else None
                    if not uin and not uid:return None
                    person=self._library().person_for_sender(self._account,uin=uin,uid=uid)
                    if person:
                        if uid and person['uid'] and uid!=person['uid']:return None
                        return person['member_id']
                    return 'uin:'+uin if uin else 'uid:'+uid
                rendered['quote'],rendered['mentions']=projection(row,resolve)
        return rendered

    def _revision(self, user):
        value = self._library().revision(self._account, user)
        return value[0] if value is not None else 1

    @guarded_read
    def project_message(self,row,revision=1):
        if row['account_key']!=self._account:
            raise AccountChangedError()
        return self._row(row,revision)

    @guarded_read
    def member_filter(self,user,member):
        if self.conversation_kind(user)!='group':
            raise ValueError('member-filter-requires-group')
        person=next((row for row in self.members(user) if row['id']==member),None)
        if person is None:
            raise ValueError('member-not-in-conversation')
        return person['uin'],person['uid']

    @guarded_read
    def messages(self, user, limit, offset=0):
        with self._library().lock:
            rows, more = self._library().latest(self._account, user, limit, offset)
            revision = self._revision(user)
            return MessageWindow([rendered for row in rows if (rendered := self._row(row, revision))], more)

    @guarded_read
    def message_windows(self, users, limit=80, expected_account=None):
        account = self._account
        if expected_account is not None and expected_account != account:
            raise AccountChangedError()
        store = self._library()
        windows, more = {}, {}
        with store.lock:
            for user in users:
                rows, has_more = store.latest(account, user, limit)
                revision = self._revision(user)
                windows[user] = [r for row in rows if (r := self._row(row, revision))]
                more[user] = has_more
        return MessageWindowBatch(windows, more)

    @guarded_read
    def texts_for_refs(self, user, refs, with_ids=False):
        return self._library().texts_for_refs(self._account, user, refs, with_ids=with_ids)

    def media(self, user, stable_id):
        self.media_reason.value = MEDIA_REASON
        return None

    # -- history -----------------------------------------------------------------

    @guarded_read
    def history_highwater(self, user):
        return self._library().highwater(self._account, user)

    @guarded_read
    def history_page(self, user, highwater, after=None, page_size=256):
        if highwater is None:
            return [], None
        with self._library().lock:
            rows, next_cursor = self._library().page(self._account, user, after, page_size,
                                                    highwater=highwater)
            revision = self._revision(user)
            return [r for row in rows if (r := self._row(row, revision))], next_cursor

    @guarded_read
    def quoted_history_page(self, user, ceiling, after=None, page_size=64, member=None):
        group=self.conversation_kind(user)=='group'
        if group and member is not None:
            self.member_filter(user,member)
        if member is not None and not group and member!=SELF_SUBJECT:
            raise ValueError("QQ single chats have no members")
        if ceiling is None:
            return [], None
        with self._library().lock:
            rows, next_cursor = self._library().page(self._account, user, after, page_size,
                                                    highwater=ceiling, quoted_only=True)
            revision = self._revision(user)
            rendered=[r for row in rows if (r:=self._row(row,revision))]
            if member is not None:
                rendered=[item for item in rendered if (item['senderId']==member if group else item['side']=='self')]
            return rendered,next_cursor

    @guarded_read
    def preceding_text_context(self, user, before, limit=3):
        with self._library().lock:
            position = decode_cursor(before, self._account, user, self._revision(user)) if isinstance(before, str) else before
            rows = self._library().before(self._account, user, position, limit)
        if self.conversation_kind(user)=='group':
            return [self._row(row,self._revision(user)) for row in rows]
        return [{"id": row["native_id"],
                 "side": "self" if row["direction"] == "self" else "other",
                 "text": row["text"] or ""} for row in rows if row["text"]]

    # -- statistics and portraits --------------------------------------------------

    @guarded_read
    def stats(self, user, member=None):
        if self.conversation_kind(user)=='group':
            if member is not None:
                person=next((row for row in self.members(user) if row['id']==member),None)
                if person is None:
                    raise ValueError('member-not-in-conversation')
                total,text=self._library().target_counts(self._account,user,uin=person['uin'],uid=person['uid'])
                return total,text,self.members(user)
            total=self._library().counts(self._account,user)[0]
            text=self._library().target_counts(self._account,user)[1]
            return total,text,self.members(user)
        # Single chat only: a group member query has no meaning here and is not faked.
        if member==SELF_SUBJECT:
            total,text_count=self._library().target_counts(self._account,user,direction='self')
            return total,text_count,[]
        if member is not None:
            raise ValueError("QQ single chats have no members")
        total, text_count = self._library().counts(self._account, user)
        return total, text_count, []

    @guarded_read
    def profile_metadata(self, user, member=None):
        if self.conversation_kind(user)=='group':
            if member is None:
                total,text,members=self.stats(user)
                return {'contact':self.contact(user),'members':members,'count':total,'textCount':text}
            total,text,members=self.stats(user,member)
            person=next(item for item in members if item['id']==member)
            return {'contact':person,'members':members,'count':total,'textCount':text}
        if member==SELF_SUBJECT:
            count,text_count=self._library().target_counts(self._account,user,direction='self')
            return {'contact':self._self_contact(),'members':[],'count':count,'textCount':text_count}
        if member is not None:
            raise ValueError("QQ single chats have no members")
        count, text_count = self._library().counts(self._account, user, peer_only=True)
        return {"contact": self.contact(user), "members": [], "count": count,
                "textCount": text_count}

    @guarded_read
    def profile_overview(self, user, member=None, highwater=None):
        if self.conversation_kind(user)=='group':
            if member is None:
                return {'contact':self.contact(user),'members':self.members(user),
                    'count':self._library().counts(self._account,user,highwater)[0],'textCount':None}
            members=self.members(user)
            person=next((row for row in members if row['id']==member),None)
            if person is None:
                raise ValueError('member-not-in-conversation')
            count,_=self._library().target_counts(self._account,user,uin=person['uin'],uid=person['uid'],highwater=highwater)
            return {'contact':person,'members':members,'count':count,'textCount':None}
        # The overview deliberately does not recount text (source contract 2.3).
        if member==SELF_SUBJECT:
            count,_=self._library().target_counts(self._account,user,direction='self',highwater=highwater)
            return {'contact':self._self_contact(),'members':[],'count':count,'textCount':None}
        if member is not None:
            raise ValueError("QQ single chats have no members")
        return {"contact": self.contact(user), "members": [],
                "count": self._library().counts(self._account, user, highwater, peer_only=True)[0], "textCount": None}

    def invalidate_profile_metadata(self, user=None):
        # QQ derives every portrait figure from the library on read, so there is no
        # private cache to clear; the public entry must still exist and be callable.
        return None

    # -- coverage and lifecycle ----------------------------------------------------

    @guarded_read
    def conversation_coverage(self, conversation_key):
        return self._library().coverage(self._account, conversation_key)

    def forget_account(self, account):
        with self.lock:
            if account != self._account:
                return
            if self._store is not None:
                self._store.close()
            self._generation += 1
            self._store = None
            self._uin = None
            self._account = None

    def attach(self, uin, store=None):
        """Bind a verified account; disk deletion stays with account_store (1.5)."""
        with self.lock:
            if self._closed:
                raise RuntimeError("QQ source is closed")
            candidate_uin = canonical_uin(uin)
            candidate_account = account_key(candidate_uin)
            if self._profile.key == "qq":
                from qq_account_store import QQAccountStore
                if QQAccountStore(self._profile.data_root(self._root)).is_deleting(candidate_account):
                    raise AccountUnavailableError()
            if (store is None and candidate_account == self._account and
                    self._store is not None and not self._store.closed):
                return self._account
            candidate = store if store is not None else open_store(
                candidate_uin, self._root, self._profile)
            try:
                candidate.assert_account(candidate_account)
            except BaseException:
                if store is None:
                    candidate.close()
                raise
            previous = self._store
            self._uin, self._account, self._store = candidate_uin, candidate_account, candidate
            self._generation += 1
            if previous is not None and previous is not candidate:
                previous.close()
            return self._account

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            if isinstance(self._store, QQMessageStore):
                self._store.close()
            self._store = None

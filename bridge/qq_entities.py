"""QQ conversation, person and membership identities; no network or model IO."""
from __future__ import annotations
import re
from urllib.parse import urlsplit
from qq_identity import canonical_uin

UID = re.compile(r"u_[A-Za-z0-9_-]{1,128}\Z")
DDL = """
CREATE TABLE qq_people_v1 (
 account_key TEXT NOT NULL, member_id TEXT NOT NULL,
 uin TEXT, uid TEXT, nickname TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(account_key,member_id), UNIQUE(account_key,uin), UNIQUE(account_key,uid)
);
CREATE TABLE qq_memberships_v1 (
 account_key TEXT NOT NULL, conversation_key TEXT NOT NULL, member_id TEXT NOT NULL,
 card_name TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 source TEXT NOT NULL DEFAULT 'observed',
 PRIMARY KEY(account_key,conversation_key,member_id),
 FOREIGN KEY(account_key,conversation_key) REFERENCES conversations(account_key,conversation_key),
 FOREIGN KEY(account_key,member_id) REFERENCES qq_people_v1(account_key,member_id)
);
CREATE TABLE qq_conversation_profiles_v1 (
 account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
 name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '', source_version TEXT,
 PRIMARY KEY(account_key,conversation_key),
 FOREIGN KEY(account_key,conversation_key) REFERENCES conversations(account_key,conversation_key)
);
"""


def conversation_identity(key, kind="friend", peer_uid=None, group_code=None):
    if kind == "friend":
        if not isinstance(key, str) or not key.startswith("u:") or not UID.fullmatch(key[2:]):
            raise ValueError("friend-conversation-invalid")
        if group_code is not None or (peer_uid is not None and peer_uid != key[2:]):
            raise ValueError("conversation-identity-mismatch")
        return key[2:], None
    if kind == "group":
        code = canonical_uin(group_code)
        if key != "g:" + code or peer_uid is not None:
            raise ValueError("group-conversation-invalid")
        return None, code
    raise ValueError("conversation-kind-invalid")


def avatar(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        return ""
    if not re.fullmatch(r"(?:p|q[1-9])\.qlogo\.cn", parsed.netloc):
        return ""
    return value


def qq_avatar(uin, *, group=False):
    number = canonical_uin(uin)
    return f"https://p.qlogo.cn/gh/{number}/{number}/640/" if group else f"https://q1.qlogo.cn/g?b=qq&nk={number}&s=640"


def identity_fields(uin=None, uid=None):
    uin = canonical_uin(uin) if uin is not None else None
    if uid is not None and (not isinstance(uid, str) or not UID.fullmatch(uid)):
        raise ValueError("member-identity-invalid")
    if uin is None and uid is None:
        raise ValueError("member-identity-unavailable")
    return uin, uid


def upsert_person(cursor, account, *, uin=None, uid=None, nickname="", avatar_url=""):
    uin, uid = identity_fields(uin, uid)
    matches = cursor.execute("SELECT * FROM qq_people_v1 WHERE account_key=? AND "
        "((? IS NOT NULL AND uin=?) OR (? IS NOT NULL AND uid=?))", (account, uin, uin, uid, uid)).fetchall()
    if len(matches) > 1:
        raise ValueError("member-identity-conflict")
    if matches:
        row = matches[0]
        if (uin and row['uin'] and row['uin'] != uin) or (uid and row['uid'] and row['uid'] != uid):
            raise ValueError("member-identity-conflict")
        member = row['member_id']
    else:
        member = "uin:" + uin if uin else "uid:" + uid
    if not isinstance(nickname, str) or len(nickname) > 256:
        raise ValueError("member-name-invalid")
    cursor.execute("INSERT INTO qq_people_v1(account_key,member_id,uin,uid,nickname,avatar_url) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(account_key,member_id) DO UPDATE SET uin=COALESCE(uin,excluded.uin),"
        "uid=COALESCE(uid,excluded.uid),nickname=CASE WHEN excluded.nickname<>'' THEN excluded.nickname ELSE nickname END,"
        "avatar_url=CASE WHEN excluded.avatar_url<>'' THEN excluded.avatar_url ELSE avatar_url END",
        (account,member,uin,uid,nickname,avatar(avatar_url)))
    return member

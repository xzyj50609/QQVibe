"""Production QCE single-chat reader. Loopback, fixed endpoints and bounded work only."""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import qce_protocol as protocol
from qq_identity import canonical_uin
from qq_normalize import COUNTER_FIELDS, NormalizationError, UID, normalize_message


class ConnectorError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SyncCancelled(Exception):
    pass


@dataclass(frozen=True)
class SyncPolicy:
    """Provisional limits; real-device calibration and G1 remain separate."""
    page_size: int = 50
    batch_size: int = 200
    max_pages: int = 3
    max_messages: int = 600
    max_seconds: float = 15
    request_timeout: float = 5
    response_bytes: int = 8 * 1024 * 1024
    overlap_ms: int = 2000
    recent_ms: int = 10 * 60 * 1000
    window_ms: int = 10 * 60 * 1000
    focused_seconds: float = 4
    background_seconds: float = 20
    max_attempts: int = 3
    requests_per_minute: int = 120
    history_seconds: float = 30
    reconcile_seconds: float = 60

    def __post_init__(self):
        for name, maximum in (("page_size", 2000), ("batch_size", 5000), ("max_pages", 20),
                              ("max_messages", 10000), ("response_bytes", 32 * 1024 * 1024),
                              ("overlap_ms", 60000), ("recent_ms", 86400000),
                              ("window_ms", 86400000), ("max_attempts", 5)):
            protocol.positive_int(name, getattr(self, name), maximum=maximum)
        protocol.positive_int("requests_per_minute", self.requests_per_minute, maximum=360)
        for name in ("max_seconds", "request_timeout", "focused_seconds", "background_seconds", "history_seconds", "reconcile_seconds"):
            protocol.positive_finite(name, getattr(self, name))
        if self.max_seconds > 60 or self.request_timeout > 10 or min(self.focused_seconds, self.background_seconds, self.history_seconds, self.reconcile_seconds) < 1:
            raise ValueError("invalid-sync-policy")

    def budget(self, attempt=0):
        if type(attempt) is not int or not 0 <= attempt < self.max_attempts:
            raise ValueError("invalid-sync-attempt")
        scale = 2 ** attempt
        fetches = min(20, self.max_pages * scale)
        return protocol.Budget(max_total_requests=fetches + 4, max_fetch_requests=fetches,
            max_pages=fetches, max_messages=min(10000, self.max_messages * scale),
            max_seconds=self.max_seconds, request_timeout=self.request_timeout)


class ReadClient(protocol.Client):
    def __init__(self, base, token, budget, response_bytes, check, on_request):
        super().__init__(base, token, budget, max_response_bytes=response_bytes)
        self.check = check
        self.on_request = on_request

    def request(self, kind, method, path, label, *, body=None):
        self.check()
        parsed = urlsplit(path)
        group_path = re.fullmatch(r'/api/groups/([1-9][0-9]*)(/members)?',parsed.path)
        allowed = (method == "POST" and path == "/api/messages/fetch") or (method == 'GET' and group_path is not None) or (
            method == "GET" and parsed.path in {"/api/system/info", "/api/system/status", "/api/friends", "/api/users/lookup"})
        if not allowed or parsed.scheme or parsed.netloc or parsed.fragment:
            raise ConnectorError("readonly-endpoint-required")
        if method == "GET":
            query = parse_qs(parsed.query, keep_blank_values=True)
            keys = {"page", "limit"} if parsed.path == "/api/friends" else {"uin"} if parsed.path == "/api/users/lookup" else ({'forceRefresh'} if group_path and group_path[2] and parsed.query else set())
            if set(query) != keys or any(len(values) != 1 for values in query.values()):
                raise ConnectorError("readonly-endpoint-required")
            if parsed.path == "/api/friends":
                if any(not value[0].isdigit() for value in query.values()) or not 1 <= int(query["page"][0]) <= 100 or not 1 <= int(query["limit"][0]) <= 50:
                    raise ConnectorError("readonly-endpoint-required")
            if parsed.path == "/api/users/lookup":
                try:
                    canonical_uin(query["uin"][0])
                except ValueError:
                    raise ConnectorError("peer-identity-invalid") from None
            if group_path and parsed.query and query.get('forceRefresh') not in (['false'],['true']):
                raise ConnectorError('readonly-endpoint-required')
        if method == "POST":
            if not isinstance(body, dict) or set(body) != {"peer", "page", "limit", "batchSize", "filter"}:
                raise ConnectorError("readonly-endpoint-required")
            peer, window = body["peer"], body["filter"]
            if (not isinstance(peer, dict) or set(peer) != {"chatType", "peerUid"} or
                    type(peer["chatType"]) is not int or peer["chatType"] not in (1,2) or
                    not isinstance(peer["peerUid"], str) or not (
                        UID.fullmatch(peer["peerUid"]) if peer['chatType']==1 else re.fullmatch(r'[1-9][0-9]{0,127}',peer['peerUid'])) or
                    not isinstance(window, dict) or set(window) != {"startTime", "endTime"} or
                    any(type(value) is not int or value < 0 for value in window.values()) or
                    window["startTime"] > window["endTime"]):
                raise ConnectorError("readonly-endpoint-required")
            for name, maximum in (("page", 1000000), ("limit", 2000), ("batchSize", 5000)):
                if type(body[name]) is not int or not 1 <= body[name] <= maximum:
                    raise ConnectorError("readonly-endpoint-required")
        self.on_request()
        result = super().request(kind, method, path, label, body=body)
        self.check()
        return result


def failure_code(error):
    if isinstance(error, ConnectorError):
        return error.code
    if isinstance(error, protocol.BudgetExhausted):
        return "budget-exhausted"
    if isinstance(error, protocol.ProbeFailure):
        if error.stage == "http" and "HTTP 429" in error.detail:
            return "rate-limited"
        return {"auth": "auth-required", "http": "connection-unavailable",
                "protocol": "protocol-invalid", "business": "source-rejected",
                "config": "configuration-invalid"}.get(error.stage, "connection-unavailable")
    return "connection-unavailable"


def partial_code(reason):
    if not reason:
        return None
    for marker, code in (("max-", "budget-exhausted"), ("duplicate", "page-repeated"),
                         ("repeated", "page-repeated"), ("empty", "page-empty-with-more"),
                         ("malformed", "invalid-message"), ("missing", "pagination-missing"),
                         ("exceeds", "page-oversized")):
        if marker in reason:
            return code
    return "source-partial"


class QQConnector:
    def __init__(self, base, token, *, policy=None):
        self.base = protocol.validate_base(base)
        if (not isinstance(token, str) or not token or len(token) > 4096 or
                any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise ConnectorError("configuration-invalid")
        self._token = token
        self.policy = policy or SyncPolicy()
        self.accepted_unverified_version=None

    def client(self, *, check=lambda: None, attempt=0, on_request=lambda: None):
        return ReadClient(self.base, self._token, self.policy.budget(attempt), self.policy.response_bytes, check, on_request)

    @staticmethod
    def identity(client):
        _, payload, _ = client.request("get", "GET", "/api/system/info", "system-info")
        info, _ = protocol.unwrap_envelope(payload, "system-info")
        napcat = info.get("napcat")
        if not isinstance(napcat, dict) or napcat.get("online") is not True:
            raise ConnectorError("qq-offline")
        self_info = napcat.get("selfInfo")
        try:
            owner = canonical_uin(self_info.get("uin") if isinstance(self_info, dict) else None)
        except ValueError:
            raise ConnectorError("identity-unavailable") from None
        _, payload, _ = client.request("get", "GET", "/api/system/status", "system-status")
        status, _ = protocol.unwrap_envelope(payload, "system-status")
        if "loggedIn" in status and status["loggedIn"] is not True:
            raise ConnectorError("qq-offline")
        from qq_compatibility import identify
        try:compatibility=identify(info,status)
        except ValueError as error:raise ConnectorError(str(error)) from None
        version=compatibility['qceVersion']
        uid = self_info.get("uid")
        return {"ownerUin": owner, "ownerUid": uid if isinstance(uid, str) and UID.fullmatch(uid) else None,
                "version": version, "name": str(self_info.get('nick') or owner)[:256],
                'compatibility':compatibility}

    def _checked_identity(self,client):
        value=self.identity(client)
        compatibility=value.get('compatibility')
        if compatibility and compatibility['requiresConsent'] and self.accepted_unverified_version!=value['version']:
            raise ConnectorError('unverified-version')
        return value

    def scan(self, owner, peer_uid, start_ms, end_ms, *, peer_uin=None, attempt=0, check=lambda: None, on_request=lambda: None, max_seconds=None, kind='friend'):
        owner = canonical_uin(owner)
        if kind not in ('friend','group'):
            raise ConnectorError('conversation-kind-invalid')
        group=kind=='group'
        if not isinstance(peer_uid, str) or not (re.fullmatch(r'[1-9][0-9]{0,127}',peer_uid) if group else UID.fullmatch(peer_uid)):
            raise ConnectorError("peer-identity-invalid")
        if group and peer_uin is not None:
            raise ConnectorError('conversation-identity-mismatch')
        if any(type(value) is not int or value < 0 for value in (start_ms, end_ms)) or start_ms > end_ms:
            raise ValueError("invalid-sync-window")
        if peer_uin is not None:
            peer_uin = canonical_uin(peer_uin)
        client = self.client(check=check, attempt=attempt, on_request=on_request)
        if max_seconds is not None:
            protocol.positive_finite("max_seconds", max_seconds)
            client.budget.max_seconds = min(client.budget.max_seconds, max_seconds)
        records, rejected, counts = [], 0, {name: 0 for name in COUNTER_FIELDS}
        try:
            before = self._checked_identity(client)
            if before["ownerUin"] != owner:
                raise ConnectorError("account-changed")
            def observe(rows, _metadata):
                nonlocal rejected
                for raw in rows:
                    check()
                    counts["rowsTotal"] += 1
                    try:
                        if not isinstance(raw, dict):
                            raise NormalizationError("not-an-object")
                        scoped = dict(raw)
                        scoped.setdefault("peerUid", peer_uid)
                        scoped.setdefault("chatType", 2 if group else 1)
                        record, flags = normalize_message(scoped, self_uin=owner,expected_kind=kind,self_uid=before.get('ownerUid'))
                        if record["conversation_key"] != ("g:" if group else "u:") + peer_uid or not start_ms <= record["time_ms"] <= end_ms:
                            raise NormalizationError("invalid-source-scope")
                        if peer_uin and record["direction"] == "peer" and canonical_uin(record["sender_uin"]) != peer_uin:
                            raise NormalizationError("invalid-source-scope")
                    except (NormalizationError, ValueError):
                        rejected += 1
                        counts["rowsRejected"] += 1
                        continue
                    records.append(record)
                    counts["rowsConflict" if record["status"] == "conflict" else "rowsOk"] += 1
                    counts["unknownTypes"] += int(flags["unknownType"])
                    counts["directionConflicts"] += int(flags["directionConflict"])
                    counts["unknownRecall"] += int(flags["unknownRecall"])
            result = protocol.bounded_fetch(client,
                SimpleNamespace(peer_uid=peer_uid, batch_size=self.policy.batch_size, limit=self.policy.page_size,chat_type=2 if group else 1),
                (start_ms, end_ms), on_page=observe)
            after = self._checked_identity(client)
            if after["ownerUin"] != owner or (before["ownerUid"] and after["ownerUid"] and before["ownerUid"] != after["ownerUid"]):
                raise ConnectorError("account-changed")
            if before['version']!=after['version']:
                raise ConnectorError('protocol-invalid')
            if rejected and before.get('compatibility',{}).get('requiresConsent'):
                raise ConnectorError('protocol-invalid')
            check()
        except (protocol.ProbeFailure, protocol.BudgetExhausted) as error:
            raise ConnectorError(failure_code(error)) from None
        compatibility=before.get('compatibility')
        if compatibility:
            compatibility={**compatibility,'capabilities':{**compatibility['capabilities'],
                'stableMessageIdentity':'checked' if records and not rejected else 'not-proven',
                'boundedPagination':'checked' if result['status'] in ('complete','complete-empty') else 'not-proven'}}
        return {"records": records, "counts": counts, 'compatibility':compatibility,
                "status": "partial" if rejected else result["status"],
                "reason": "normalization-rejected" if rejected else partial_code(result["partialReason"]),
                "windowStartMs": start_ms, "windowEndMs": end_ms, "version": before["version"],
                "requests": client.budget.used_total, "fetchRequests": client.budget.used_fetch}

    def contacts(self, owner, *, page=1, check=lambda: None, on_request=lambda: None):
        owner = canonical_uin(owner)
        protocol.positive_int("page", page, maximum=100)
        client = self.client(check=check, on_request=on_request)
        if self._checked_identity(client)["ownerUin"] != owner:
            raise ConnectorError("account-changed")
        _, payload, _ = client.request("get", "GET", f"/api/friends?page={page}&limit=50", "friends")
        data, _ = protocol.unwrap_envelope(payload, "friends")
        rows = data.get("friends")
        if not isinstance(rows, list) or len(rows) > 50:
            raise ConnectorError("protocol-invalid")
        contacts = []
        for row in rows:
            if not isinstance(row, dict):
                raise ConnectorError("protocol-invalid")
            try:
                uin = canonical_uin(row.get("uin"))
            except ValueError:
                raise ConnectorError("peer-identity-invalid") from None
            uid = row.get("uid", row.get("peerUid"))
            contacts.append({"uin": uin, "peerUid": uid if isinstance(uid, str) and UID.fullmatch(uid) else None,
                             "name": str(row.get("remark") or row.get("nickname") or row.get('nick') or uin)[:256]})
        check()
        if self._checked_identity(client)["ownerUin"] != owner:
            raise ConnectorError("account-changed")
        return {"contacts": contacts, "page": page, "mayHaveMore": len(rows) == 50}

    def resolve_peer(self, owner, peer_uin, *, check=lambda: None, on_request=lambda: None):
        owner, peer_uin = canonical_uin(owner), canonical_uin(peer_uin)
        client = self.client(check=check, on_request=on_request)
        if self._checked_identity(client)["ownerUin"] != owner:
            raise ConnectorError("account-changed")
        _, payload, _ = client.request("get", "GET", "/api/users/lookup?uin=" + peer_uin, "peer-lookup")
        data, _ = protocol.unwrap_envelope(payload, "peer-lookup")
        uid = data.get("uid", data.get("peerUid"))
        if data.get("uid") is not None and data.get("peerUid") is not None and data["uid"] != data["peerUid"]:
            raise ConnectorError("peer-identity-invalid")
        if "uin" in data and data["uin"] != peer_uin:
            raise ConnectorError("peer-identity-invalid")
        if data.get("found") is False or data.get("isFriend") is False or not isinstance(uid, str) or not UID.fullmatch(uid):
            raise ConnectorError("peer-identity-invalid")
        check()
        if self._checked_identity(client)["ownerUin"] != owner:
            raise ConnectorError("account-changed")
        from qq_entities import qq_avatar
        return {"peerUin": peer_uin, "peerUid": uid, "conversationKey": "u:" + uid,
                "name":str(data.get('remark') or data.get('nick') or peer_uin)[:256],"avatar":qq_avatar(peer_uin)}

    def group_metadata(self,owner,group_code,*,check=lambda:None,on_request=lambda:None):
        owner,code=canonical_uin(owner),canonical_uin(group_code)
        client=self.client(check=check,on_request=on_request)
        if self._checked_identity(client)['ownerUin']!=owner:
            raise ConnectorError('account-changed')
        _,payload,_=client.request('get','GET','/api/groups/'+code,'selected-group-detail')
        detail,_=protocol.unwrap_envelope(payload,'selected-group-detail')
        try:
            if canonical_uin(detail.get('groupCode'))!=code:
                raise ValueError()
        except ValueError:
            raise ConnectorError('group-identity-invalid') from None
        _,payload,_=client.request('get','GET','/api/groups/'+code+'/members?forceRefresh=false','selected-group-members')
        if not isinstance(payload,dict) or payload.get('success') is not True or not isinstance(payload.get('data'),list):
            raise ConnectorError('protocol-invalid')
        raw_members=payload['data']
        if len(raw_members)>10000:
            raise ConnectorError('protocol-invalid')
        from qq_entities import identity_fields,qq_avatar
        members=[]
        try:
            for raw in raw_members:
                uin,uid=identity_fields(raw.get('uin'),raw.get('uid') or None)
                members.append({'uin':uin,'uid':uid,'nick':str(raw.get('nick') or '')[:256],
                    'cardName':str(raw.get('cardName') or '')[:256], 'isDelete':raw.get('isDelete') is True,
                    'avatarUrl':qq_avatar(uin) if uin else ''})
        except (ValueError,AttributeError):
            raise ConnectorError('member-identity-invalid') from None
        check()
        if self._checked_identity(client)['ownerUin']!=owner:
            raise ConnectorError('account-changed')
        return {'conversationKey':'g:'+code,'kind':'group','groupCode':code,
            'name':str(detail.get('groupName') or code)[:256],'avatar':qq_avatar(code,group=True),'members':members}

"""QCE raw message row -> canonical message record (migration contract T04).

Pure normalization functions never read a database, QQ client or the network.
The explicit export-file entry reads only the supplied local export and bounded,
contained chunk paths. Every rule here is recorded in ``docs/migration/data-schema.md``
section 1 and section 6, so the normalizer can be tested from input to output
rather than by pre-seeding a status.

Field shapes taken from the fixed QCE 6.3.0 source (``api/routes/messages.rs``):
``senderUin``/``msgTime``/``chatType`` arrive either as JSON strings or as numbers,
``msgId``/``msgSeq`` arrive as strings. Raw messages use ``elements[].textElement``;
exported CleanMessage uses ``content.text`` and separate reply metadata.
Anything this module cannot map is counted and rejected; it is never guessed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from qq_identity import canonical_uin

NORMALIZE_VERSION = "qq-v3"
SUPPORTED_QCE_VERSION = "6.3.0"
UNKNOWN_PLACEHOLDER = "[未知类型]"

# data-schema section 1: only this mapping has evidence. Every other value stays
# a counted unknown instead of being renamed into a plausible kind.
MSG_TYPE_KIND = {2: "text"}
# T01 section 8.2 measured on the live connector; used only to cross-check direction.
SEND_TYPE_DIRECTION = {"2": "self", "0": "peer", "3": "system"}
CHAT_TYPE_FRIEND = 1

DIGITS = re.compile(r"[0-9]+")
CONTROL_CHARS = re.compile(r"[\u0000-\u001f\u007f]")
UID = re.compile(r"u_[A-Za-z0-9_-]{1,128}\Z")
# A second-level epoch is 10 digits; 13 or more is a millisecond value in disguise.
MAX_SECONDS_DIGITS = 11

COUNTER_FIELDS = ("rowsTotal", "rowsOk", "rowsRejected", "rowsConflict", "unknownTypes",
                  "unknownRecall", "directionConflicts", "pendingIdentity",
                  "groupRejected", "invalidTime", "invalidId")
# A rejection reason is reported verbatim and also raises one named counter (V08).
REASON_COUNTERS = {
    "not-an-object": "invalidId",
    "unsupported-chat-type": "groupRejected",
    "invalid-chat-type": "invalidId",
    "pending-identity": "pendingIdentity",
    "invalid-time": "invalidTime",
    "time-unit-ambiguous": "invalidTime",
    "invalid-native-id": "invalidId",
    "invalid-sender": "invalidId",
    "invalid-self-identity": "invalidId",
    "invalid-body": "invalidId",
    "invalid-quote": "invalidId",
    "invalid-json-row": "invalidId",
}


class NormalizationError(ValueError):
    """A row could not be turned into a canonical record; the reason is counted."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _text(value):
    """IDs stay strings end to end (V03): a JSON number is kept verbatim, never re-parsed."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int):
        text = str(value)
    else:
        return None
    if not text or CONTROL_CHARS.search(text):
        return None
    return text


def _digits(value):
    text = _text(value)
    return text if text is not None and DIGITS.fullmatch(text) else None


def _signed(value):
    """Recall timestamps are integers, and a negative one is a shape defect, not absent."""
    text = _text(value)
    return text if text is not None and re.fullmatch(r"[+-]?[0-9]+", text) else None


def conversation_key(row, *, expected_kind="friend"):
    """data-schema section 5: the first release anchors a single chat on peerUid only."""
    try:
        chat_type = int(_digits(row.get("chatType")))
    except (TypeError, ValueError):
        raise NormalizationError("invalid-chat-type")
    if expected_kind == 'group':
        if chat_type != 2:
            raise NormalizationError('unsupported-chat-type')
        try:
            return 'g:' + canonical_uin(row.get('peerUid'))
        except ValueError:
            raise NormalizationError('pending-identity') from None
    if expected_kind != 'friend' or chat_type != CHAT_TYPE_FRIEND:
        # X7: group-shaped input must not be filtered down into a two-person chat.
        raise NormalizationError("unsupported-chat-type")
    peer_uid = _text(row.get("peerUid"))
    if peer_uid is None or not UID.fullmatch(peer_uid):
        raise NormalizationError("pending-identity")
    return "u:" + peer_uid


def time_ms(value):
    """The one and only seconds->milliseconds conversion point (V06)."""
    digits = _digits(value)
    if digits is None:
        raise NormalizationError("invalid-time")
    if len(digits.lstrip("0") or "0") >= MAX_SECONDS_DIGITS:
        raise NormalizationError("time-unit-ambiguous")
    return int(digits) * 1000


def direction(row, self_uin):
    """Primary rule is the sender UIN; sendType only cross-checks it (C04/V05)."""
    sender_uin = _digits(row.get("senderUin"))
    send_type = _text(row.get("sendType"))
    if send_type is not None:
        send_type = send_type.lstrip("0") or "0"
    claimed = SEND_TYPE_DIRECTION.get(send_type) if send_type is not None else None
    if claimed == "system":
        return "system", sender_uin, False
    try:
        primary = "self" if canonical_uin(sender_uin) == canonical_uin(self_uin) else "peer"
    except ValueError:
        raise NormalizationError("invalid-sender")
    if claimed is not None and claimed != primary:
        # Neither branch wins: keep the row, mark it, and let the caller count it.
        return "conflict", sender_uin, True
    return primary, sender_uin, False


def recall(raw):
    """data-schema section 6.2, the only recall rule: strictly positive means recalled."""
    if raw is None or (isinstance(raw, str) and raw.strip() in ("", "null", "None")):
        return "keep", None, True
    if isinstance(raw, bool):
        return "keep", _text(raw), True
    digits = _signed(raw)
    if digits is None:
        return "keep", _text(raw), True
    value = int(digits)
    if value > 0:
        return "recalled", digits, False
    if value == 0:
        return "keep", digits, False
    return "conflict", digits, True


def kind_of(row):
    """The message kind is a versioned table lookup; unmapped values are counted (V08)."""
    raw_type = row.get("msgType", row.get("type"))
    try:
        msg_type = int(_digits(raw_type))
    except (TypeError, ValueError):
        return "unknown", None, True
    kind = MSG_TYPE_KIND.get(msg_type)
    if kind is None:
        return "unknown", msg_type, True
    return kind, msg_type, False


def body(row, kind):
    """Body text only for kinds we can actually name; media elements are placeholders."""
    if kind != "text":
        return None
    # Raw QCE/NapCat data has no CleanMessage content wrapper. Only text elements
    # belong to the author's body; reply/face/media payloads must never be flattened.
    if "elements" in row:
        elements = row["elements"]
        if not isinstance(elements, list):
            raise NormalizationError("invalid-body")
        parts = []
        for element in elements:
            if not isinstance(element, dict):
                raise NormalizationError("invalid-body")
            if "textElement" in element:
                value = element["textElement"]
                if value is None:
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                    raise NormalizationError("invalid-body")
                parts.append(value["content"])
        return "".join(parts) if parts else None
    text = row.get("text")  # Kept for the already-declared v1 flat-record adapter.
    if text is None:
        elements = (row.get("content") or {}).get("elements") if isinstance(row.get("content"), dict) else None
        if isinstance(elements, list):
            texts = [element.get("text") for element in elements
                     if isinstance(element, dict) and element.get("type") == "text"
                     and isinstance(element.get("text"), str)]
            text = "\n".join(texts) if texts else None
    if text is not None and not isinstance(text, str):
        raise NormalizationError("invalid-body")
    return text


def quote_of(row):
    quote = row.get("quote")
    if quote is None:
        # The raw reply element references an optional already-included record.
        # Never fetch missing quoted messages or guess their author/body.
        for element in row.get("elements", []) if isinstance(row.get("elements"), list) else []:
            reply = element.get("replyElement") if isinstance(element, dict) else None
            if not isinstance(reply, dict):
                continue
            referenced = _text(reply.get("sourceMsgIdInRecords"))
            records = row.get("records")
            for candidate in records if isinstance(records, list) else []:
                if isinstance(candidate, dict) and referenced and _text(candidate.get("msgId")) == referenced:
                    return body(candidate, "text")
        return None
    if isinstance(quote, dict):
        quote = quote.get("text")
    if not isinstance(quote, str):
        raise NormalizationError("invalid-quote")
    # Quoted text is display context only: it never counts as the peer's own writing (R15).
    return quote


def normalize_message(row, *, self_uin, expected_kind="friend", self_uid=None):
    """Return one canonical record, or raise NormalizationError with the counted reason."""
    if not isinstance(row, dict):
        raise NormalizationError("not-an-object")
    try:
        self_uin = canonical_uin(self_uin)
    except ValueError:
        raise NormalizationError("invalid-self-identity")
    native_id = _text(row.get("msgId"))
    if native_id is None:
        raise NormalizationError("invalid-native-id")
    # msgSeq is kept exactly as supplied and is never widened to an integer (RS06/V03).
    native_seq = _text(row.get("msgSeq"))
    record = {
        "native_id_kind": "qce-msgId",
        "native_id": native_id,
        "native_seq": native_seq,
        "conversation_key": conversation_key(row, expected_kind=expected_kind),
        "time_ms": time_ms(row.get("msgTime")),
        "sender_uid": _text(row.get("senderUid")),
        "send_type": _text(row.get("sendType")),
        "msg_type": None,
        "kind": "unknown",
        "text": None,
        "quote": quote_of(row),
        "status": "normal",
        "recall_time": None,
        "raw": json.dumps(row, ensure_ascii=False, sort_keys=True),
        "normalize_version": NORMALIZE_VERSION,
    }
    try:
        valid_group_uin = canonical_uin(row.get('senderUin'))
    except ValueError:
        valid_group_uin = None
    if expected_kind=='group':
        if record.get('sender_uid')=='':
            record['sender_uid']=None
        if not isinstance(self_uid,str) or not UID.fullmatch(self_uid):
            self_uid=None
    if expected_kind == 'group' and not valid_group_uin:
        uid = _text(row.get('senderUid'))
        if _text(row.get('sendType')) == '3':
            record['direction'],sender_uin,conflicted = 'system',None,False
        elif uid and UID.fullmatch(uid) and self_uid:
            record['direction'],sender_uin,conflicted = ('self' if uid == self_uid else 'peer'),None,False
        else:
            record['direction'],sender_uin,conflicted = 'conflict',None,True
    else:
        record["direction"], sender_uin, conflicted = direction(row, self_uin)
    record["sender_uin"] = sender_uin
    kind, msg_type, unknown_type = kind_of(row)
    record["kind"] = kind
    record["msg_type"] = msg_type
    record["text"] = body(row, kind)
    if kind == "text" and record["text"] is None:
        # A media-only/unsupported element row survives as a counted placeholder,
        # never as a successful text row with silently missing body.
        record["kind"], unknown_type = "unknown", True
    action, recall_raw, unknown_recall = recall(row.get("recallTime"))
    record["recall_time"] = recall_raw
    if action == "recalled":
        record["status"] = "recalled"
    elif action == "conflict" or conflicted:
        record["status"] = "conflict"
    flags = {"unknownType": unknown_type, "unknownRecall": unknown_recall,
             "directionConflict": conflicted}
    return record, flags


def normalize_messages(rows, *, self_uin, expected_kind="friend", self_uid=None):
    """Batch entry point: conserved counters, per-row reasons, and no body in errors."""
    records, rejected = [], []
    counts = {name: 0 for name in COUNTER_FIELDS}
    for index, row in enumerate(rows):
        counts["rowsTotal"] += 1
        try:
            record, flags = normalize_message(row, self_uin=self_uin, expected_kind=expected_kind, self_uid=self_uid)
        except NormalizationError as error:
            counts["rowsRejected"] += 1
            counts[REASON_COUNTERS.get(error.reason, "invalidId")] += 1
            rejected.append({"index": index, "reason": error.reason})
            continue
        if flags["unknownType"]:
            counts["unknownTypes"] += 1
        if flags["unknownRecall"]:
            counts["unknownRecall"] += 1
        if flags["directionConflict"]:
            counts["directionConflicts"] += 1
        if flags["directionConflict"] or record["status"] == "conflict":
            counts["rowsConflict"] += 1
        else:
            counts["rowsOk"] += 1
        records.append(record)
    if counts["rowsTotal"] != counts["rowsOk"] + counts["rowsRejected"] + counts["rowsConflict"]:
        raise AssertionError("normalized batch is not conserved")
    return {"records": records, "rejected": rejected, "counts": counts}


class ExportFormatError(ValueError):
    """Fixed reason codes only: never embed an export path, row or message body."""


def detect_export_format(document):
    if not isinstance(document, dict):
        raise ExportFormatError("invalid-export-container")
    # A single export renamed manifest.json is still a single export.
    if isinstance(document.get("messages"), list):
        return "qce-single-json"
    chunked = document.get("chunked")
    if isinstance(chunked, dict) and chunked.get("format") == "jsonl" and isinstance(chunked.get("chunks"), list):
        return "qce-chunked-jsonl"
    raise ExportFormatError("unsupported-export-format")


def chunk_paths(document):
    chunked = document["chunked"]
    directory = chunked.get("chunksDir", "chunks")
    names = []
    for entry in chunked["chunks"]:
        if not isinstance(entry, dict):
            raise ExportFormatError("invalid-chunk-entry")
        name = entry.get("relativePath")
        if name is None:
            leaf = entry.get("fileName", entry.get("file"))
            if not isinstance(directory, str) or not isinstance(leaf, str):
                raise ExportFormatError("invalid-chunk-path")
            name = directory + "/" + leaf
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name or
                CONTROL_CHARS.search(name) or PurePosixPath(name).is_absolute() or
                ".." in PurePosixPath(name).parts or name in names or
                re.search(r'[<>"|?*]', name) or
                any(part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in
                    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))} for part in PurePosixPath(name).parts)):
            raise ExportFormatError("invalid-chunk-path")
        names.append(name)
    return names


def _export_timestamp(value):
    if not isinstance(value, str):
        raise NormalizationError("invalid-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        milliseconds = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
        if milliseconds < 0:
            raise ValueError("pre-epoch")
        return milliseconds
    except (ValueError, OverflowError):
        raise NormalizationError("invalid-time")


def normalize_export_row(row, owner, peer_uid, *, expected_kind='friend', self_uid=None):
    if not isinstance(row, dict):
        raise NormalizationError("not-an-object")
    sender, content = row.get("sender"), row.get("content")
    if not isinstance(sender, dict) or not isinstance(content, dict):
        raise NormalizationError("invalid-body")
    for flag in ("system", "recalled", "isSystemMessage", "isRecalled"):
        if flag in row and type(row[flag]) is not bool:
            raise NormalizationError("invalid-body")
    system = row.get("system", row.get("isSystemMessage", False))
    recalled = row.get("recalled", row.get("isRecalled", False))
    timestamp = _export_timestamp(row.get("timestamp"))
    quote = content.get("reply")
    if quote is not None and not isinstance(quote, dict):
        raise NormalizationError("invalid-quote")
    authored_text = content.get("text")
    elements = content.get("elements")
    if elements is not None and not isinstance(elements, list):
        raise NormalizationError("invalid-body")
    if elements:
        # QCE's content.text is a rendered preview (including media/reply labels).
        # Structured text data is the author's body; everything else stays raw.
        pieces = []
        for element in elements:
            if not isinstance(element, dict) or not isinstance(element.get("type"), str):
                raise NormalizationError("invalid-body")
            data = element.get("data")
            if element["type"] == "text":
                if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                    raise NormalizationError("invalid-body")
                pieces.append(data["text"])
            elif element["type"] == "reply" and quote is None and isinstance(data, dict):
                quote = data if isinstance(data.get("content"), str) else None
        authored_text = "".join(pieces) if pieces else None
    raw = {"msgId": row.get("messageId"), "msgSeq": None, "msgTime": str(timestamp // 1000),
           "chatType": 2 if expected_kind=='group' else 1, "peerUid": peer_uid, "senderUin": sender.get("uin"),
           "senderUid": sender.get("uid"), "sendType": "3" if system else None,
           "msgType": row.get("messageType"), "text": authored_text,
           "quote": quote.get("content") if quote else None, "recallTime": None}
    record, flags = normalize_message(raw, self_uin=owner,expected_kind=expected_kind,self_uid=self_uid)
    record["time_ms"] = timestamp
    record["raw"] = json.dumps(row, ensure_ascii=False, sort_keys=True)
    record["normalize_version"] = NORMALIZE_VERSION + ":qce-export-v6"
    if recalled:
        record["status"] = "recalled"
    # The export contains a recall boolean, not a recall timestamp. Do not invent one.
    flags["unknownRecall"] = False
    return record, flags


def normalize_export(document, *, self_uin=None, chunks=None, max_rows=200000, expected_kind='friend'):
    """Normalize a QCE V6 single JSON or manifest + supplied JSONL chunks.

    Explicit selfUin metadata or a caller-confirmed owner is mandatory. Participants
    are checked across the entire container before accepting any single-chat row.
    The function is pure; ``read_export`` is the separate bounded file entry.
    """
    format_kind = detect_export_format(document)
    metadata, info = document.get("metadata"), document.get("chatInfo")
    if not isinstance(metadata, dict) or not isinstance(info, dict):
        raise ExportFormatError("invalid-export-metadata")
    version = metadata.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"6\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ExportFormatError("unsupported-export-version")
    if expected_kind not in ('friend','group'):
        raise ExportFormatError('invalid-conversation-kind')
    if expected_kind=='group' and info.get('type')!='group':
        raise ExportFormatError('not-group-chat')
    if expected_kind=='friend' and info.get("type") not in (None, "private", "friend"):
        raise ExportFormatError("not-single-chat")
    declared = info.get("selfUin")
    try:
        owner = canonical_uin(self_uin if self_uin is not None else declared)
        if declared is not None and canonical_uin(declared) != owner:
            raise ExportFormatError("owner-mismatch")
    except ValueError as exc:
        if isinstance(exc, ExportFormatError):
            raise
        return {"status": "pending-identity", "reason": "owner-required", "format": format_kind,
                "records": [], "rejected": [], "counts": {name: 0 for name in COUNTER_FIELDS}}
    if type(max_rows) is not int or max_rows < 1:
        raise ExportFormatError("invalid-row-budget")
    if format_kind == "qce-single-json":
        rows = document["messages"]
    else:
        rows = []
        chunks = chunks or {}
        for name in chunk_paths(document):
            if name not in chunks:
                raise ExportFormatError("missing-chunk")
            lines = chunks[name]
            if not isinstance(lines, (str, list, tuple)):
                raise ExportFormatError("invalid-chunk-content")
            if isinstance(lines, str):
                lines = lines.splitlines()
            for line in lines:
                if isinstance(line, str):
                    if not line.strip():
                        continue
                    try:
                        line = json.loads(line)
                    except (ValueError, TypeError):
                        line = None
                rows.append(line)
                if len(rows) > max_rows:
                    raise ExportFormatError("row-budget-exceeded")
    if len(rows) > max_rows:
        raise ExportFormatError("row-budget-exceeded")
    if expected_kind=='group':
        try:
            code=canonical_uin(info.get('peerUid'))
            if len(code)>128:
                raise ValueError('invalid group identity')
        except ValueError:
            return {'status':'pending-identity','reason':'group-code-required','format':format_kind,
                    'records':[],'rejected':[],'counts':{name:0 for name in COUNTER_FIELDS}}
        result={'records':[],'rejected':[],'counts':{name:0 for name in COUNTER_FIELDS}}
        for index,row in enumerate(rows):
            counts=result['counts'];counts['rowsTotal']+=1
            try:
                record,flags=normalize_export_row(row,owner,code,expected_kind='group',self_uid=info.get('selfUid'))
            except NormalizationError as exc:
                counts['rowsRejected']+=1
                counts[REASON_COUNTERS.get(exc.reason,'invalidId')]+=1
                result['rejected'].append({'index':index,'reason':exc.reason})
                continue
            counts['unknownTypes']+=int(flags['unknownType'])
            counts['unknownRecall']+=int(flags['unknownRecall'])
            counts['directionConflicts']+=int(flags['directionConflict'])
            counts['rowsConflict' if record['status']=='conflict' or flags['directionConflict'] else 'rowsOk']+=1
            result['records'].append(record)
        return {**result,'status':'partial' if result['rejected'] else 'complete','format':format_kind,'kind':'group',
                'ownerUin':owner,'peerUid':None,'peerUin':None,'groupCode':code,'conversationKey':'g:'+code,'sourceVersion':version}
    peers, peer_uids = set(), set()
    for row in rows:
        if not isinstance(row, dict) or row.get("system", row.get("isSystemMessage", False)) is True:
            continue
        sender = row.get("sender")
        if not isinstance(sender, dict):
            continue
        try:
            uin = canonical_uin(sender.get("uin"))
        except ValueError:
            continue
        if uin != owner:
            peers.add(uin)
            if _text(sender.get("uid")):
                peer_uids.add(_text(sender["uid"]))
    if len(peers) > 1 or len(peer_uids) > 1:
        raise ExportFormatError("not-single-chat")
    peer_uid = _text(info.get("peerUid")) or next(iter(peer_uids), None)
    if peer_uid is None or not UID.fullmatch(peer_uid):
        return {"status": "pending-identity", "reason": "peer-uid-required", "format": format_kind,
                "records": [], "rejected": [], "counts": {name: 0 for name in COUNTER_FIELDS}}
    if peer_uids and peer_uid not in peer_uids:
        raise ExportFormatError("peer-identity-mismatch")
    if info.get("peerUin") is not None and peers:
        try:
            matches = canonical_uin(info["peerUin"]) in peers
        except ValueError:
            matches = False
        if not matches:
            raise ExportFormatError("peer-identity-mismatch")
    result = {"records": [], "rejected": [], "counts": {name: 0 for name in COUNTER_FIELDS}}
    for index, row in enumerate(rows):
        counts = result["counts"]
        counts["rowsTotal"] += 1
        try:
            record, flags = normalize_export_row(row, owner, peer_uid)
        except NormalizationError as exc:
            counts["rowsRejected"] += 1
            counts[REASON_COUNTERS.get(exc.reason, "invalidId")] += 1
            result["rejected"].append({"index": index, "reason": exc.reason})
            continue
        counts["unknownTypes"] += int(flags["unknownType"])
        counts["unknownRecall"] += int(flags["unknownRecall"])
        counts["directionConflicts"] += int(flags["directionConflict"])
        counts["rowsConflict" if record["status"] == "conflict" or flags["directionConflict"] else "rowsOk"] += 1
        result["records"].append(record)
    return {**result, "status": "partial" if result["rejected"] else "complete", "format": format_kind,
            "ownerUin": owner, "peerUin": next(iter(peers), None), "peerUid": peer_uid,
            "conversationKey": "u:" + peer_uid, "sourceVersion": version}


def read_export(path, *, self_uin=None, max_rows=200000, max_bytes=128 * 1024 * 1024):
    """Read one user-selected export, following only bounded paths inside its directory."""
    path = Path(path).resolve()
    remaining = max_bytes
    def read(candidate):
        nonlocal remaining
        with candidate.open("rb") as stream:
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise ExportFormatError("byte-budget-exceeded")
        remaining -= len(data)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ExportFormatError("invalid-export-encoding")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ExportFormatError("invalid-byte-budget")
    try:
        document = json.loads(read(path))
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ExportFormatError):
            raise
        raise ExportFormatError("invalid-export-json")
    chunks = {}
    if detect_export_format(document) == "qce-chunked-jsonl":
        for name in chunk_paths(document):
            candidate = (path.parent / name).resolve()
            if not candidate.is_relative_to(path.parent):
                raise ExportFormatError("chunk-outside-export-directory")
            chunks[name] = read(candidate)
    return normalize_export(document, self_uin=self_uin, chunks=chunks, max_rows=max_rows)

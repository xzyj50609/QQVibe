"""Explicit QQ JSON adapters and user field mappings, independent of databases."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from qq_identity import canonical_uin
from qq_normalize import ExportFormatError, NormalizationError, UID

FIELD_ALIASES = {
    'text': ('content.text', 'text', 'message', 'content', 'body', 'msg', '消息内容', '内容'),
    'sender': ('sender.uin', 'sender.user_id', 'senderUin', 'senderId', 'sender_id', 'user_id', 'from_id', 'qq', 'sender', '发送者', '发言人'),
    'time': ('timestamp', 'msgTime', 'time', 'datetime', 'date', 'createTime', 'created_at', 'sendTime', '时间', '发送时间'),
    'id': ('messageId', 'msgId', 'message_id', 'platformMessageId', 'id', '消息ID'),
    'senderName': ('sender.name', 'sender.nickname', 'sender.card', 'sendNickName', 'nickname', 'name', '昵称'),
    'senderUid': ('sender.uid', 'senderUid', 'sender_uid'),
}
OPTIONS = {*FIELD_ALIASES, 'recordPath', 'timeUnit', 'timeZone', 'kind', 'peerUid',
           'peerUin', 'groupCode', 'name', 'senders'}


def validate_options(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - OPTIONS:
        raise ValueError('invalid-json-mapping')
    result = {}
    for key, item in value.items():
        if key == 'senders':
            if not isinstance(item, dict) or len(item) > 200:
                raise ValueError('invalid-json-mapping')
            mapped = {}
            for name, number in item.items():
                if not isinstance(name, str) or not 1 <= len(name) <= 256:
                    raise ValueError('invalid-json-mapping')
                mapped[name] = canonical_uin(number)
            result[key] = mapped
        else:
            if not isinstance(item, str) or len(item) > 256 or any(ord(c) < 32 for c in item):
                raise ValueError('invalid-json-mapping')
            if item:
                result[key] = item
    if result.get('timeUnit', 'auto') not in ('auto', 'seconds', 'milliseconds'):
        raise ValueError('invalid-json-mapping')
    if result.get('kind', 'auto') not in ('auto', 'friend', 'group'):
        raise ValueError('invalid-json-mapping')
    if result.get('recordPath') and not result['recordPath'].startswith('/'):
        raise ValueError('invalid-json-mapping')
    if result.get('timeZone'):
        zone(result['timeZone'])
    return result


def zone(value):
    if value in ('Z', 'UTC'):
        return timezone.utc
    if not isinstance(value, str) or not re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d', value):
        raise ValueError('invalid-json-timezone')
    minutes = int(value[1:3]) * 60 + int(value[4:])
    if minutes > 840:
        raise ValueError('invalid-json-timezone')
    return timezone(timedelta(minutes=minutes if value[0] == '+' else -minutes))


def get(row, path):
    parts = [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')] if path.startswith('/') else path.split('.')
    for part in parts:
        if isinstance(row, dict):
            row = row.get(part)
        elif isinstance(row, list) and part.isdigit() and int(part) < len(row):
            row = row[int(part)]
        else:
            return None
    return row


def field(row, name, options, *, qce=False):
    if options.get(name):
        return get(row, options[name])
    if name == 'time' and qce and isinstance(row.get('sender'), dict) and isinstance(row.get('content'), dict):
        # QCE documents define timestamp as authoritative and time as display
        # text. This source-specific rule does not relax unknown JSON conflicts.
        if row.get('timestamp') is not None:
            return row['timestamp']
    values = []
    for path in FIELD_ALIASES[name]:
        value = get(row, path)
        if value is None or isinstance(value, bool):
            continue
        if name == 'text':
            if not isinstance(value, (str, list)):
                continue
        elif not isinstance(value, (str, int, float)):
            continue
        if name == 'time':
            # Native seconds plus a formatted display time are not two clocks.
            if path == 'msgTime':
                return value
        values.append(value)
        # Source IDs and display nicknames can have more than one alias. The
        # documented priority is intentional; message/time/sender are stricter.
        if name in ('id', 'senderName', 'senderUid'):
            return value
    if not values:
        return None
    if name == 'text':
        texts = [text_content(value) for value in values]
        # Structured segments carry the authored body; raw CQ/rendered previews
        # must not introduce image links or reply labels into analysis.
        structured = [text_content(value) for value in values if isinstance(value, list)]
        if structured:
            return next(value for value in values if isinstance(value, list))
        values = texts
    if any(str(value) != str(values[0]) for value in values[1:]):
        raise NormalizationError('ambiguous-' + name + '-field')
    return values[0]


def text_content(value):
    if isinstance(value, str):
        # Plain text stays verbatim. Do not interpret HTML/JS or CQ markup.
        return value
    if isinstance(value, list):
        pieces = []
        for item in value:
            if not isinstance(item, dict):
                raise NormalizationError('invalid-body')
            if item.get('type') == 'text':
                data = item.get('data', item)
                text = data.get('text') if isinstance(data, dict) else None
                if not isinstance(text, str):
                    raise NormalizationError('invalid-body')
                pieces.append(text)
            elif 'textElement' in item and isinstance(item['textElement'], dict):
                text = item['textElement'].get('content')
                if not isinstance(text, str):
                    raise NormalizationError('invalid-body')
                pieces.append(text)
        return ''.join(pieces) if pieces else None
    return None


def timestamp(value, options):
    if isinstance(value, bool) or value is None:
        raise NormalizationError('invalid-time')
    if isinstance(value, (int, float)) or isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?', value):
        try:
            number = Decimal(str(value))
            unit = options.get('timeUnit', 'auto')
            if not number.is_finite() or number < 0:
                raise ValueError()
            if unit == 'auto':
                if number < 100000000000:
                    unit = 'seconds'
                elif number >= 1000000000000:
                    unit = 'milliseconds'
                else:
                    raise NormalizationError('time-unit-ambiguous')
            milliseconds = int(number * 1000 if unit == 'seconds' else number)
            parsed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds)
        except NormalizationError:
            raise
        except (ValueError, InvalidOperation, OverflowError):
            raise NormalizationError('invalid-time') from None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            raise NormalizationError('invalid-time') from None
        if parsed.tzinfo is None:
            if not options.get('timeZone'):
                raise NormalizationError('time-zone-required')
            parsed = parsed.replace(tzinfo=zone(options['timeZone']))
    else:
        raise NormalizationError('invalid-time')
    return parsed.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def native_document(document, options):
    metadata, info = document.get('metadata'), document.get('chatInfo')
    return (not options and isinstance(metadata, dict) and isinstance(info, dict)
            and isinstance(metadata.get('version'), str)
            and re.fullmatch(r'6\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', metadata['version']))


def qce_time_options(row, options):
    if (isinstance(row.get('timestamp'), (int, float)) and options.get('time', 'timestamp') in ('timestamp', '/timestamp')
            and options.get('timeUnit', 'auto') == 'auto'):
        if type(row['timestamp']) is not int:
            raise NormalizationError('invalid-time')
        return {**options, 'timeUnit': 'milliseconds'}
    return options


def header(document, options):
    declared = document.get('chatInfo')
    if declared is not None and not isinstance(declared, dict):
        raise ExportFormatError('invalid-export-metadata')
    info = dict(declared or {})
    meta = document.get('meta')
    meta = meta if isinstance(meta, dict) else {}
    platform = str(meta.get('platform') or '').lower()
    if platform and platform != 'qq':
        raise ExportFormatError('non-qq-export')
    if meta.get('name'):
        info.setdefault('name', meta['name'])
    if meta.get('type') in ('group', 'private', 'friend'):
        info.setdefault('type', meta['type'])
    if meta.get('groupId'):
        info.setdefault('type', 'group')
        info.setdefault('peerUid', str(meta['groupId']))
    kind = options.get('kind', 'auto')
    if kind != 'auto':
        declared_kind = 'friend' if info.get('type') in ('friend', 'private') else info.get('type')
        if declared_kind and declared_kind != kind:
            raise ExportFormatError('conversation-kind-mismatch')
        info['type'] = kind
    for target, source in (('peerUid', 'peerUid'), ('peerUin', 'peerUin'), ('name', 'name')):
        if options.get(source):
            if target != 'name' and info.get(target) and info[target] != options[source]:
                raise ExportFormatError('peer-identity-mismatch')
            info[target] = options[source]
    if options.get('groupCode'):
        if info.get('type') in ('friend', 'private'):
            raise ExportFormatError('conversation-kind-mismatch')
        code = canonical_uin(options['groupCode'])
        if info.get('peerUid') and str(info['peerUid']) != code:
            raise ExportFormatError('peer-identity-mismatch')
        info.update(type='group', peerUid=code)
    return info


def adapt(row, options, *, snapshot, index, qce=False, chatlab=False):
    if not isinstance(row, dict):
        raise NormalizationError('not-an-object')
    # A QCE clean row is already losslessly understood; pre-V6 flags are also
    # handled by normalize_export_row. Only its timestamp may need conversion.
    if qce and isinstance(row.get('sender'), dict) and isinstance(row.get('content'), dict) and (row.get('messageId') or row.get('id')) and not any(k in options for k in FIELD_ALIASES if k != 'time'):
        result = dict(row)
        result['messageId'] = row.get('messageId', row.get('id'))
        result['timestamp'] = timestamp(field(row, 'time', options, qce=True), qce_time_options(row, options))
        return result, 'qce-msgId'
    source_sender = field(row, 'sender', options)
    name = field(row, 'senderName', options)
    if source_sender is not None:
        source_sender = str(source_sender)
    sender = options.get('senders', {}).get(source_sender, source_sender)
    system = row.get('system', row.get('isSystemMessage', False))
    recalled = row.get('recalled', row.get('isRecalled', False))
    if type(system) is not bool or type(recalled) is not bool:
        raise NormalizationError('invalid-body')
    if chatlab:
        system = system or row.get('type') == 80
        recalled = recalled or row.get('type') == 81
    try:
        sender = canonical_uin(sender)
    except ValueError:
        if not system:
            raise NormalizationError('sender-mapping-required') from None
        sender = None
    uid = field(row, 'senderUid', options)
    if uid is not None and (not isinstance(uid, str) or not UID.fullmatch(uid)):
        raise NormalizationError('invalid-sender')
    raw_native = 'msgId' in row and 'msgTime' in row
    if raw_native and isinstance(row.get('elements'), list) and not options.get('text'):
        text = text_content(row['elements'])
    else:
        text = text_content(field(row, 'text', options))
    # ChatLab TEXT=0, SYSTEM=80, RECALL=81. Media paths and rendered labels
    # stay placeholders, not invented author speech. Other numeric type tables
    # are not assumed equivalent to QQ's raw msgType table.
    if chatlab and row.get('type') != 0:
        text = None
    elif isinstance(row.get('type'), str) and row['type'].lower() in ('image', 'video', 'audio', 'voice', 'file', 'face', 'sticker', 'system', 'recall'):
        text = None
    if text is None and not any(k in row for k in ('messageType', 'msgType', 'type', 'message_type', 'elements', 'message')):
        raise NormalizationError('text-field-required')
    identifier = field(row, 'id', options)
    if identifier is None or identifier == '':
        identifier, id_kind = f'{snapshot}:{index}', 'json-file-row'
    elif isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
        identifier = str(identifier)
        id_kind = 'qce-msgId' if raw_native or qce else 'onebot-message-id' if 'message_id' in row else 'chatlab-message-id' if chatlab else 'json-message-id'
    else:
        raise NormalizationError('invalid-native-id')
    body = {'text': text}
    if isinstance(row.get('content'), dict):
        for key in ('elements', 'reply'):
            if key in row['content']:
                body[key] = row['content'][key]
    time_options = qce_time_options(row, options) if qce else options
    result = {'messageId': identifier, 'timestamp': timestamp(field(row, 'time', options, qce=qce), time_options),
              'sender': {'uin': sender, 'uid': uid, 'name': str(name or source_sender or '')[:128]},
              'messageType': 2 if text is not None else 0, 'content': body,
              'system': system, 'recalled': recalled}
    return result, id_kind


def row_scope(row):
    """Return declared conversation scope, excluding peer IDs in nested replies."""
    if not isinstance(row, dict):
        return None
    if row.get('group_id') is not None:
        return 'group', canonical_uin(row['group_id'])
    if str(row.get('chatType')) == '2':
        return 'group', canonical_uin(row.get('peerUid'))
    if str(row.get('chatType')) == '1' and row.get('peerUid'):
        return 'friend', str(row['peerUid'])
    return None


def fields(row, prefix='', depth=0):
    if not isinstance(row, dict) or depth > 5:
        return []
    result = []
    for key, value in row.items():
        path = prefix + str(key)
        if isinstance(value, dict):
            result.extend(fields(value, path + '.', depth + 1))
        else:
            result.append(path[:256])
        if len(result) >= 256:
            break
    return result[:256]

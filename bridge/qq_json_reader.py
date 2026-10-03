"""Streaming discovery for non-QCE JSON containers. Never evaluates source code."""
from __future__ import annotations

import json
from pathlib import Path

from qq_import_reader import Budget, Tokens, TOKEN_LIMIT
from qq_normalize import ExportFormatError

ARRAY_NAMES = {'messages', 'records', 'rows', 'history', 'list', 'data', 'items',
               'logs', 'messageList', 'chatHistory', 'chat_history', '聊天记录', '消息', '记录'}
HEADERS = {'metadata', 'chatInfo', 'meta', 'chatlab'}


def pointer(parts):
    return ''.join('/' + part.replace('~', '~0').replace('/', '~1') for part in parts)


def skip(tokens, depth=0):
    """Discard optional metadata incrementally, still validating JSON syntax."""
    if depth > 64:
        raise ExportFormatError('export-nesting-too-deep')
    char = tokens.peek()
    if char in ('{', '['):
        end = '}' if char == '{' else ']'
        tokens.expect(char)
        if tokens.peek() != end:
            while True:
                if char == '{':
                    if not isinstance(tokens.value(), str):
                        raise ExportFormatError('invalid-export-json')
                    tokens.expect(':')
                skip(tokens, depth + 1)
                if tokens.peek() == end:
                    break
                tokens.expect(',')
        tokens.expect(end)
    elif char == '"':
        # An unused embedded avatar can be much larger than a message token.
        tokens.expect('"')
        escaped, unicode_left = False, 0
        while True:
            if not tokens.buffer and not tokens.fill():
                raise ExportFormatError('invalid-export-json')
            for index, char in enumerate(tokens.buffer):
                if unicode_left:
                    if char not in '0123456789abcdefABCDEF':
                        raise ExportFormatError('invalid-export-json')
                    unicode_left -= 1
                elif escaped:
                    if char == 'u':
                        unicode_left = 4
                    elif char not in '"\\/bfnrt':
                        raise ExportFormatError('invalid-export-json')
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    tokens.buffer = tokens.buffer[index + 1:]
                    return
                elif ord(char) < 32:
                    raise ExportFormatError('invalid-export-json')
            tokens.buffer = ''
    else:
        tokens.value()


def stream_json(path, on_row, *, cancel, max_bytes, record_path=''):
    """Return small headers + candidate paths; retain at most one message in RAM.

    Multiple candidate arrays are reported, never silently concatenated. A user
    can select one JSON Pointer and re-preview. No nested messages in replies or
    forwarded payloads are promoted to the conversation's top-level messages.
    """
    budget = Budget(max_bytes, cancel)
    document, candidates, selected = {}, [], []
    jsonl = Path(path).suffix.lower() in ('.jsonl', '.ndjson')
    if not jsonl:
        # Some exporters name newline-delimited JSON *.json. Probe complete
        # lines, never infer JSONL from a pretty-printed object's opening brace.
        with Budget(max_bytes, cancel).open(path) as probe:
            first = probe.readline(TOKEN_LIMIT + 1)
            while first and not first.strip():
                first = probe.readline(TOKEN_LIMIT + 1)
            try:
                first_row = json.loads(first, parse_constant=Tokens.bad_constant)
            except (ValueError, TypeError):
                first_row = None
            if isinstance(first_row, dict) and not any(k in first_row for k in ('messages', 'chunked', 'chatInfo')):
                second = probe.readline(TOKEN_LIMIT + 1)
                while second and not second.strip():
                    second = probe.readline(TOKEN_LIMIT + 1)
                jsonl = bool(second)
    if jsonl:
        line_number, chatlab = 0, False
        with budget.open(path) as stream:
            while True:
                line = stream.readline(TOKEN_LIMIT + 1)
                if not line:
                    break
                if len(line) > TOKEN_LIMIT:
                    raise ExportFormatError('export-item-too-large')
                if not line.strip():
                    continue
                if chatlab and line.lstrip().startswith('#'):
                    continue
                line_number += 1
                try:
                    value = json.loads(line, parse_constant=Tokens.bad_constant)
                except (ValueError, TypeError):
                    on_row(None, 'invalid-json-line')
                else:
                    if isinstance(value, dict) and value.get('_type') == 'header' and isinstance(value.get('chatlab'), dict):
                        if line_number != 1:
                            raise ExportFormatError('multiple-conversations')
                        document.update({key: value[key] for key in ('chatlab', 'meta') if key in value})
                        chatlab = True
                    elif chatlab and isinstance(value, dict) and value.get('_type') == 'member':
                        continue
                    elif chatlab and (not isinstance(value, dict) or value.get('_type') != 'message'):
                        on_row(value, 'invalid-json-row')
                    else:
                        on_row(value, None)
        budget.verify()
        document.update(messages=[], _arrayPaths=['$lines'])
        return document, 'generic-jsonl'

    with budget.open(path) as stream:
        tokens = Tokens(stream, cancel)

        def walk(parts=(), depth=0):
            if depth > 16:
                skip(tokens)
                return
            char = tokens.peek()
            location = pointer(parts) or '/'
            if char == '[':
                if len(candidates) >= 128:
                    raise ExportFormatError('too-many-json-arrays')
                candidates.append(location)
                use = location == record_path if record_path else not parts or parts[-1] in ARRAY_NAMES
                if use:
                    selected.append(location)
                tokens.expect('[')
                if tokens.peek() != ']':
                    while True:
                        if use and len(selected) == 1:
                            on_row(tokens.value(), None)
                        else:
                            skip(tokens)
                        if tokens.peek() == ']':
                            break
                        tokens.expect(',')
                tokens.expect(']')
            elif char == '{':
                tokens.expect('{')
                seen = set()
                if tokens.peek() != '}':
                    while True:
                        key = tokens.value()
                        if not isinstance(key, str) or key in seen:
                            raise ExportFormatError('invalid-export-json')
                        seen.add(key)
                        if len(seen) > 10000:
                            raise ExportFormatError('export-item-too-large')
                        tokens.expect(':')
                        if not parts and key in HEADERS:
                            document[key] = tokens.value()
                        elif key in ('avatars', 'statistics', 'exportOptions') and not record_path.startswith(pointer((*parts, key)) + '/'):
                            skip(tokens)
                        elif tokens.peek() in ('{', '['):
                            walk((*parts, key), depth + 1)
                        else:
                            skip(tokens)
                        if tokens.peek() == '}':
                            break
                        tokens.expect(',')
                tokens.expect('}')
            else:
                raise ExportFormatError('unsupported-export-format')

        walk()
        if tokens.peek():
            raise ExportFormatError('invalid-export-json')
    budget.verify()
    document.update(messages=[], _arrayPaths=candidates, _selectedArrays=selected)
    return document, 'generic-json'

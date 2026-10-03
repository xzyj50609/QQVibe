"""Bounded local export streaming. No network or live-library writes."""
from __future__ import annotations

import json
import io
import os
from contextlib import contextmanager
from pathlib import Path

from account_store import _check_root, _regular
from qq_normalize import ExportFormatError, chunk_paths

TOKEN_LIMIT = 2 * 1024 * 1024


class CheckedText:
    def __init__(self, stream, size, cancel, byte_encoding='utf-8'):
        self.stream, self.size, self.cancel = stream, size, cancel
        self.byte_encoding = byte_encoding
        self.used = 0

    def _read(self, method, amount):
        self.cancel()
        value = method(amount)
        self.used += len(value.encode(self.byte_encoding))
        if self.used > self.size:
            raise ExportFormatError("export-changed-during-read")
        return value

    def read(self, amount):
        return self._read(self.stream.read, amount)

    def readline(self, amount):
        return self._read(self.stream.readline, amount)


class Budget:
    def __init__(self, maximum, cancel):
        self.remaining = maximum
        self.cancel = cancel
        self.files = []

    @contextmanager
    def open(self, path):
        self.cancel()
        path = Path(os.path.abspath(path))
        _check_root(path.parent)
        if not _regular(path):
            raise ExportFormatError("missing-export-file")
        before = path.stat()
        if before.st_size > self.remaining:
            raise ExportFormatError("byte-budget-exceeded")
        self.remaining -= before.st_size
        stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        with path.open('rb') as raw:
            prefix = raw.read(4)
            raw.seek(0)
            encoding, byte_encoding = 'utf-8-sig', 'utf-8'
            if prefix.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
                encoding, byte_encoding = 'utf-32', 'utf-32-le' if prefix[0] == 255 else 'utf-32-be'
            elif prefix.startswith((b'\xff\xfe', b'\xfe\xff')):
                encoding, byte_encoding = 'utf-16', 'utf-16-le' if prefix[0] == 255 else 'utf-16-be'
            with io.TextIOWrapper(raw, encoding=encoding, newline='') as stream:
                yield CheckedText(stream, before.st_size, self.cancel, byte_encoding)
                after = os.fstat(stream.fileno())
        latest = path.stat()
        if any((item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) != stamp
               for item in (after, latest)):
            raise ExportFormatError("export-changed-during-read")
        self.files.append((path, stamp))

    def verify(self):
        for path, stamp in self.files:
            self.cancel()
            _check_root(path.parent)
            if not _regular(path):
                raise ExportFormatError("export-changed-during-read")
            item = path.stat()
            if (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) != stamp:
                raise ExportFormatError("export-changed-during-read")


class Tokens:
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel
        self.buffer, self.eof = "", False
        self.decoder = json.JSONDecoder(parse_constant=self.bad_constant)

    @staticmethod
    def bad_constant(_value):
        raise ExportFormatError("invalid-export-json")

    def fill(self):
        self.cancel()
        if self.eof:
            return False
        value = self.stream.read(65536)
        self.buffer += value
        self.eof = not value
        if len(self.buffer) > TOKEN_LIMIT:
            raise ExportFormatError("export-item-too-large")
        return bool(value)

    def peek(self):
        while True:
            self.buffer = self.buffer.lstrip()
            if self.buffer or not self.fill():
                return self.buffer[:1]

    def expect(self, character):
        if self.peek() != character:
            raise ExportFormatError("invalid-export-json")
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                if end == len(self.buffer) and not self.eof:
                    self.fill()
                    continue
                if end < len(self.buffer) and self.buffer[end] not in " \r\n\t,:]}":
                    raise ExportFormatError("invalid-export-json")
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                if not self.fill():
                    raise ExportFormatError("invalid-export-json")


def stream_export(path, on_row, *, cancel, max_bytes=128 * 1024 * 1024):
    """Emit rows without retaining the messages array; metadata may follow it."""
    budget = Budget(max_bytes, cancel)
    document, seen, single = {}, set(), False
    with budget.open(path) as stream:
        tokens = Tokens(stream, cancel)
        tokens.expect("{")
        if tokens.peek() != "}":
            while True:
                name = tokens.value()
                if not isinstance(name, str) or name in seen:
                    raise ExportFormatError("invalid-export-json")
                seen.add(name)
                tokens.expect(":")
                if name == "messages":
                    single = True
                    document[name] = []
                    tokens.expect("[")
                    if tokens.peek() != "]":
                        while True:
                            on_row(tokens.value(), None)
                            if tokens.peek() == "]":
                                break
                            tokens.expect(",")
                    tokens.expect("]")
                else:
                    document[name] = tokens.value()
                if tokens.peek() == "}":
                    break
                tokens.expect(",")
        tokens.expect("}")
        if tokens.peek():
            raise ExportFormatError("invalid-export-json")
    if single and "chunked" in document:
        raise ExportFormatError("ambiguous-export-format")
    if not single:
        chunked = document.get("chunked")
        if not isinstance(chunked, dict) or chunked.get("format") != "jsonl" or not isinstance(chunked.get("chunks"), list):
            raise ExportFormatError("unsupported-export-format")
        base = Path(path).absolute().parent
        names = chunk_paths(document)
        for number, name in enumerate(names):
            candidate = base / name
            if not candidate.resolve().is_relative_to(base.resolve()):
                raise ExportFormatError("chunk-outside-export-directory")
            count = 0
            with budget.open(candidate) as stream:
                while True:
                    cancel()
                    line = stream.readline(TOKEN_LIMIT + 1)
                    if not line:
                        break
                    if len(line) > TOKEN_LIMIT:
                        raise ExportFormatError("export-item-too-large")
                    if not line.strip():
                        continue
                    count += 1
                    try:
                        row = json.loads(line, parse_constant=Tokens.bad_constant)
                    except (ValueError, TypeError):
                        on_row(None, "invalid-json-line")
                    else:
                        on_row(row, None)
            # QCE manifest shapes with declared chunk counts cannot quietly truncate.
            entry = chunked["chunks"][number]
            declared = entry.get("messageCount")
            if declared is not None and (type(declared) is not int or declared != count):
                raise ExportFormatError("chunk-count-mismatch")
    budget.verify()
    return document, "qce-single-json" if single else "qce-chunked-jsonl"

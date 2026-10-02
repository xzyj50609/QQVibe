"""UTF-8 JSONL request/response framing.

stdout carries only protocol responses; stderr carries only sanitized state codes. Every request
carries ``{id, method, params}`` and every response ``{id, ok, result|error}``.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from . import errors

PROTOCOL_VERSION = 1

MAX_LINE_BYTES = 8 * 1024 * 1024


def read_request(stream: TextIO) -> dict[str, Any] | None:
    """Read one JSONL request. Returns None on EOF. Raises ProtocolError on framing problems."""
    line = stream.readline(MAX_LINE_BYTES + 1)
    if line == "":
        return None
    if len(line.encode("utf-8", "ignore")) > MAX_LINE_BYTES or not line.endswith("\n"):
        raise errors.ProtocolError(errors.PAYLOAD_TOO_LARGE, "request line too large")
    text = line.strip()
    if not text:
        raise errors.ProtocolError(errors.BAD_JSON, "empty line")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise errors.ProtocolError(errors.BAD_JSON, f"invalid json: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise errors.ProtocolError(errors.BAD_REQUEST, "request must be an object")
    return parsed


def validate_request(request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(request_id, str) or not (1 <= len(request_id) <= 256):
        raise errors.ProtocolError(errors.BAD_REQUEST, "id must be a 1..256 string")
    if not isinstance(method, str) or not (1 <= len(method) <= 64):
        raise errors.ProtocolError(errors.BAD_REQUEST, "method must be a 1..64 string")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise errors.ProtocolError(errors.BAD_REQUEST, "params must be an object")
    return request_id, method, params


def write_message(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def ok(request_id: str, result: Any) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def fail(request_id: str, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}

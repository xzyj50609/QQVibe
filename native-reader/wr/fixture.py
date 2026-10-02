"""Synthetic fixture source.

Fixtures use the SAME contact/message query, pagination and normalization code as production; only
source discovery/decryption are bypassed (plaintext SQLite files listed in a manifest). Fixtures are
never used as a fallback success in live mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import errors


@dataclass
class FixtureShard:
    id: str
    image: bytes


@dataclass
class FixtureContact:
    id: str
    username: str
    name: str
    kind: str


@dataclass
class FixtureAccount:
    id: str
    name: str
    username: str | None
    contacts: list[FixtureContact] = field(default_factory=list)
    shards: list[FixtureShard] = field(default_factory=list)


def load_fixture(manifest_path: str) -> list[FixtureAccount]:
    manifest_path = os.path.abspath(manifest_path)
    if not os.path.isfile(manifest_path):
        raise errors.ProtocolError(errors.NOT_FOUND, "fixture manifest not found")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise errors.ProtocolError(errors.BAD_REQUEST, "fixture manifest unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise errors.ProtocolError(errors.BAD_REQUEST, "unsupported fixture manifest version")
    base = os.path.dirname(manifest_path)
    accounts: list[FixtureAccount] = []
    for raw_account in manifest.get("accounts", []):
        shards: list[FixtureShard] = []
        for raw_shard in raw_account.get("shards", []):
            path = raw_shard["path"]
            if not os.path.isabs(path):
                path = os.path.join(base, path)
            try:
                with open(path, "rb") as handle:
                    shards.append(FixtureShard(id=str(raw_shard["id"]), image=handle.read()))
            except OSError as exc:
                raise errors.ProtocolError(errors.READ_ERROR, "fixture shard unreadable") from exc
        contacts = [
            FixtureContact(
                id=str(raw.get("id") or raw["username"]),
                username=str(raw["username"]),
                name=str(raw.get("name") or raw["username"]),
                kind=str(raw.get("kind") or "friend"),
            )
            for raw in raw_account.get("contacts", [])
        ]
        accounts.append(
            FixtureAccount(
                id=str(raw_account["id"]),
                name=str(raw_account.get("name") or raw_account["id"]),
                username=raw_account.get("username"),
                contacts=contacts,
                shards=shards,
            )
        )
    return accounts

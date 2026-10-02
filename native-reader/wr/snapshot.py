"""Ciphertext snapshot store with a shared per-account dataset pool.

Two layers are persisted in the caller-provided cache directory:

* a **dataset** — the frozen ciphertext bytes of every message shard (plus its WAL) captured once
  for an (account, cutoff, source-fingerprint) batch. It never contains plaintext in live mode, so
  several contacts of the same account/cutoff share one dataset instead of copying the whole
  database per contact;
* a **snapshot** — small opaque metadata that references a dataset and carries one contact's
  cutoff/total/identity mapping.

Keys are never written. Blob file names are a hash of the shard id (never a ``replace('/')``
mangling that could collide). Snapshot/dataset ids are opaque tokens validated against traversal.
In explicit synthetic fixture mode the stored bytes are the fixture images (plaintext by design).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid

from . import errors

ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _safe_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise errors.ProtocolError(errors.SNAPSHOT_ERROR, f"invalid {what} id")
    return value


def blob_digest(shard_id: str) -> str:
    """Stable, collision-resistant blob suffix for a shard id (avoids path-separator mangling)."""
    return hashlib.sha256(shard_id.encode("utf-8")).hexdigest()[:32]


class SnapshotStore:
    def __init__(self, directory: str) -> None:
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)

    # -- paths -------------------------------------------------------------

    def _dataset_meta_path(self, dataset_id: str) -> str:
        return os.path.join(self.directory, f"{_safe_id(dataset_id, 'dataset')}.dataset.json")

    def _snapshot_meta_path(self, snapshot_id: str) -> str:
        return os.path.join(self.directory, f"{_safe_id(snapshot_id, 'snapshot')}.meta.json")

    def _blob_path(self, dataset_id: str, shard_id: str) -> str:
        return os.path.join(
            self.directory, f"{_safe_id(dataset_id, 'dataset')}.{blob_digest(shard_id)}.bin"
        )

    def _write_atomic(self, path: str, data: bytes) -> None:
        temporary = f"{path}.tmp"
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)

    # -- datasets ----------------------------------------------------------

    def has_dataset(self, dataset_id: str) -> bool:
        return os.path.isfile(self._dataset_meta_path(dataset_id))

    def create_dataset(self, dataset_id: str, meta: dict, blobs: list[tuple[str, bytes]]) -> dict:
        dataset_id = _safe_id(dataset_id, "dataset")
        for shard_id, blob in blobs:
            path = self._blob_path(dataset_id, shard_id)
            if not os.path.isfile(path):
                self._write_atomic(path, blob)
        # `meta["shards"]` (with shardId/hasWal) is authoritative; blobs are stored beside it.
        payload = {**meta, "datasetId": dataset_id}
        self._write_atomic(
            self._dataset_meta_path(dataset_id),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return payload

    def load_dataset_meta(self, dataset_id: str) -> dict:
        path = self._dataset_meta_path(dataset_id)
        if not os.path.isfile(path):
            raise errors.ProtocolError(errors.NOT_FOUND, "dataset not found")
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_dataset_blob(self, dataset_id: str, shard_id: str) -> bytes:
        path = self._blob_path(dataset_id, shard_id)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise errors.ProtocolError(errors.SNAPSHOT_ERROR, "dataset blob unreadable") from exc

    def prune_datasets(self, max_datasets: int) -> None:
        """Keep only the newest `max_datasets` datasets (bounded cache growth)."""
        try:
            entries = [
                name
                for name in os.listdir(self.directory)
                if name.endswith(".dataset.json")
            ]
        except OSError:
            return
        if len(entries) <= max_datasets:
            return
        entries.sort(key=lambda name: os.path.getmtime(os.path.join(self.directory, name)))
        for name in entries[: len(entries) - max_datasets]:
            dataset_id = name[: -len(".dataset.json")]
            try:
                os.remove(os.path.join(self.directory, name))
            except OSError:
                continue
            for other in os.listdir(self.directory):
                if other.startswith(f"{dataset_id}.") and other.endswith(".bin"):
                    try:
                        os.remove(os.path.join(self.directory, other))
                    except OSError:
                        pass

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(self, meta: dict) -> dict:
        snapshot_id = uuid.uuid4().hex
        payload = {**meta, "snapshotId": snapshot_id}
        self._write_atomic(
            self._snapshot_meta_path(snapshot_id),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return payload

    def load_meta(self, snapshot_id: str) -> dict:
        path = self._snapshot_meta_path(snapshot_id)
        if not os.path.isfile(path):
            raise errors.ProtocolError(errors.NOT_FOUND, "snapshot not found")
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

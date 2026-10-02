"""QQ API analysis: source/component-scoped drafts and revision-checked publication."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from account_store import _check_root, _regular
from backend_contracts import api_insight_scope, api_portrait_scope
from qq_analysis_store import AnalysisScope, StaleAnalysisError
from result_store import ResultStore

COMPONENTS = {"insights", "portrait", "inventory"}


def insight_context_hash(window, portrait_context):
    # Same actual wire context for every target in a QQ single chat. Cursor tokens
    # and fetch times are excluded; body, direction, order and portrait context are not.
    values = [[item["id"], item["side"], item["text"],item.get('senderId')] for item in window
              if item.get("kind") == "text" and item.get("side") in ("self", "other")
              and isinstance(item.get("text"), str)]
    return hashlib.sha256(json.dumps([values, portrait_context], ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


class QQApiStage(ResultStore):
    def __init__(self, owner, path, user, source_id, component, subject, revision, identifier):
        self.owner, self.user, self.source_id = owner, user, source_id
        self.component, self.subject = component, subject
        self.revision, self.identifier = revision, identifier
        self.control_epoch=owner.base.analysis_epoch(owner.account,user)
        self.context_hash = None
        self.portrait_context = ""
        super().__init__(path, transaction_guard=self.guard)
        self.scope = AnalysisScope(self)

    @property
    def identity(self):
        return self.owner.account, self.user, self.source_id, self.component, self.subject

    @contextmanager
    def guard(self):
        with self.owner.source.read_library(self.owner.account) as (_account, library):
            if self.component!='inventory':
                from analysis_control import require_running
                require_running(self.owner.base,self.owner.account,self.user,self.control_epoch)
            basis = library.revision(self.owner.account, self.user)
            with self.owner.base.connect() as conn:
                row = conn.execute("SELECT stage_revision,stage_id FROM qq_api_generations_v1 WHERE "
                    "account=? AND session=? AND source_id=? AND component=? AND subject=?", self.identity).fetchone()
            if (basis is None or basis[0] != self.revision or row != (self.revision, self.identifier) or
                    self.cache_suspended(self.owner.account, self.source_id)):
                raise StaleAnalysisError()
            yield

    def cache_suspended(self, account, source_id):
        return False if self.component == "inventory" else self.owner.base.cache_suspended(account, source_id)

    def register_api_source(self, account, source_id, protocol, model):
        self.owner.base.register_api_source(account, source_id, protocol, model)

    def api_insight_known(self, account, user, source_id, ids):
        if self.context_hash is None:
            return set()
        return super().api_insight_known(account, user, source_id, ids) & self.api_insight_basis_ids(
            account, user, source_id, ids, self.revision, self.context_hash)

    def save_api_insights(self, account, user, source_id, rows):
        if not isinstance(self.context_hash, str):
            raise RuntimeError("API insight context not bound")
        return super().save_api_insights(account, user, source_id, rows,
                                        basis=(self.revision, self.context_hash))

    def api_history_inventory_save(self, account, user, subject, highwater, fingerprint, available):
        if self.component == "inventory":
            return super().api_history_inventory_save(account, user, subject, highwater, fingerprint, available)
        with self.guard():
            inventory = self.owner.bind(user, "inventory", "inventory", subject)
            inventory.api_history_inventory_save(account, user, subject, highwater, fingerprint, available)
            inventory.publish()

    def api_history_inventory_get(self, account, user, subject, highwater):
        if self.component == "inventory":
            return super().api_history_inventory_get(account, user, subject, highwater)
        with self.guard():
            return self.owner.bind(user, "inventory", "inventory", subject).api_history_inventory_get(
                account, user, subject, highwater)

    def publish(self):
        self.owner.publish(self)


class QQApiAnalysisStore:
    def __init__(self, source, base, account, workdir):
        self.source, self.base, self.account = source, base, account
        self.workdir = Path(workdir)
        self.directory = self.workdir / "api-analysis-staging"

    def revision(self, user):
        with self.source.read_library(self.account) as (_account, library):
            basis = library.revision(self.account, user)
            if basis is None:
                raise ValueError("unknown QQ conversation")
            return basis[0]

    def _directory(self, source_id):
        if source_id != "inventory" and not re.fullmatch(r"[0-9a-f]{32}", source_id):
            raise ValueError("invalid API stage source")
        directory = self.directory / source_id
        _check_root(directory)
        return directory

    def status(self, user, source_id, component, subject):
        revision = self.revision(user)
        with self.base.connect() as conn:
            row = conn.execute("SELECT published_revision FROM qq_api_generations_v1 WHERE "
                "account=? AND session=? AND source_id=? AND component=? AND subject=?",
                (self.account, user, source_id, component, subject)).fetchone()
        return {"dataRevision": revision, "analysisRevision": row[0] if row else None,
                "stale": row is None or row[0] != revision}

    def bind(self, user, source_id, component, subject):
        if component not in COMPONENTS or (component == "inventory") != (source_id == "inventory"):
            raise ValueError("invalid API analysis component")
        with self.source.read_library(self.account):
            revision = self.revision(user)
            identity = self.account, user, source_id, component, subject
            directory = self._directory(source_id)
            with self.base.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT published_revision,stage_revision,stage_id FROM qq_api_generations_v1 WHERE "
                    "account=? AND session=? AND source_id=? AND component=? AND subject=?", identity).fetchone()
                published = row[0] if row else None
                identifier = row[2] if row and row[1] == revision else uuid.uuid4().hex
                if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                    raise ValueError("invalid API stage identifier")
                if row is None or row[1] != revision:
                    conn.execute("INSERT OR REPLACE INTO qq_api_generations_v1 VALUES (?,?,?,?,?,?,?,?)",
                                 (*identity, published, revision, identifier))
            path = directory / (identifier + ".sqlite3")
            _regular(path)
            directory.mkdir(parents=True, exist_ok=True)
            if not path.exists() and published == revision:
                # The stage is disposable; recover a missing one from committed data.
                with self.base.connect() as source, closing(sqlite3.connect(path)) as target:
                    source.backup(target)
            return QQApiStage(self, path, user, source_id, component, subject, revision, identifier)

    @staticmethod
    def selections(stage):
        prefix = stage.owner.account, stage.user
        if stage.component == "insights":
            return [(table, "account=? AND session=? AND source_id=?", (*prefix, api_insight_scope(stage.source_id)))
                    for table in ("api_insights_v1", "qq_api_insight_basis_v1")]
        if stage.component == "portrait":
            return [("api_portrait_v1", "account=? AND session=? AND source_id=? AND subject=?",
                     (*prefix, api_portrait_scope(stage.source_id), stage.subject))]
        return [("api_history_inventory_v1", "account=? AND session=? AND subject=?", (*prefix, stage.subject))]

    def publish(self, stage):
        with stage.guard(), self.base.profile_lock:
            if stage.component == "portrait":
                saved = stage.api_portrait_get(self.account, stage.user, api_portrait_scope(stage.source_id), stage.subject)
                if saved is None or not saved["complete"]:
                    raise RuntimeError("unfinished API portrait cannot be published")
            with self.base.connect() as conn:
                conn.execute("ATTACH DATABASE ? AS qq_stage", (str(stage.path),))
                conn.execute("BEGIN IMMEDIATE")
                for table, where, args in self.selections(stage):
                    conn.execute(f"DELETE FROM main.{table} WHERE {where}", args)
                    conn.execute(f"INSERT INTO main.{table} SELECT * FROM qq_stage.{table} WHERE {where}", args)
                conn.execute("UPDATE qq_api_generations_v1 SET published_revision=? WHERE "
                    "account=? AND session=? AND source_id=? AND component=? AND subject=? AND stage_id=?",
                    (stage.revision, *stage.identity, stage.identifier))

    def clear_source(self, source_id):
        directory = self._directory(source_id)
        if not directory.exists():
            return
        paths = list(directory.iterdir())
        for path in paths:
            if not re.fullmatch(r"[0-9a-f]{32}\.sqlite3(?:-journal|-wal|-shm)?", path.name) or not _regular(path):
                raise RuntimeError("unexpected API stage file")
        for path in paths:
            path.unlink()

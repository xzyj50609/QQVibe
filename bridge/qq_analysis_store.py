"""Revision-bound local analysis, restartable staging and atomic publication.

The published result database keeps the last successful evidence. Work for a
different QQ data revision uses a fresh database; no earlier checkpoints are
reused because the legacy batch format has no independently valid checkpoints
before an arbitrary affected position. Model calls never hold the source lock.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from account_store import _check_root, _regular
from backend_contracts import LOCAL_SOURCE_ID
from batch_state import BatchStateStore
from result_store import ResultStore

LOCAL_TABLES = {
    **{name: "version" for name in (
        "results_v2", "analysis_skips", "fine_results_v1", "fine_skips_v1",
        "progress_v1", "summary_v1", "profile_tokens_v1", "profile_tokens_ready_v1", "profile_state_v1")},
    **{name: "base_version" for name in (
        "batch_progress_v1", "batch_runs_v1", "batch_coverage_v1", "batch_fragments_v1", "quoted_backfill_v1")},
}


class StaleAnalysisError(RuntimeError):
    code = "data-revision-changed"
    def __init__(self):
        super().__init__("聊天记录已更新，本次旧分析已停止，请重试重算")


class AnalysisScope(tuple):
    def __new__(cls, stage):
        instance = super().__new__(cls, (stage.owner.account, str(stage.owner.workdir.resolve())))
        instance.stage = stage
        return instance

    def assert_current(self):
        with self.stage.guard():
            pass


class QQAnalysisStage(ResultStore):
    def __init__(self, owner, path, user, version, revision, identifier, rebuild):
        self.owner, self.user, self.version = owner, user, version
        self.revision, self.identifier, self.requires_rebuild = revision, identifier, rebuild
        self.control_epoch=owner.base.analysis_epoch(owner.account,user)
        super().__init__(path, transaction_guard=self.guard)
        self.scope = AnalysisScope(self)

    @contextmanager
    def guard(self):
        # Same lock order as ingestion: source binding, then canonical library.
        # Holding it through SQLite commit removes the check/commit race.
        with self.owner.source.read_library(self.owner.account) as (_account, library):
            from analysis_control import require_running
            require_running(self.owner.base,self.owner.account,self.user,self.control_epoch)
            basis = library.revision(self.owner.account, self.user)
            if basis is None or basis[0] != self.revision:
                raise StaleAnalysisError()
            with self.owner.base.connect() as conn:
                row = conn.execute("SELECT stage_revision,stage_id FROM qq_analysis_generations_v1 "
                    "WHERE account=? AND session=? AND version=?", self.identity).fetchone()
            if row != (self.revision, self.identifier) or self.cache_suspended(self.owner.account, LOCAL_SOURCE_ID):
                raise StaleAnalysisError()
            yield

    @property
    def identity(self):
        return self.owner.account, self.user, self.version

    def cache_suspended(self, account, source_id):
        # The account's live quiesce flag, never the stage's older snapshot.
        return self.owner.base.cache_suspended(account, source_id)

    def publish(self, *, complete=False):
        if self.requires_rebuild and not complete:
            return False
        return self.owner.publish(self)


class QQAnalysisStore:
    def __init__(self, source, base, account, workdir):
        self.source, self.base, self.account = source, base, account
        self.workdir = Path(workdir)
        self.directory = self.workdir / "analysis-staging"
        self.stages = {}
        # Install the same schema on both sides before scoped publication.
        BatchStateStore(base)

    def _stage_path(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid analysis stage identifier")
        _check_root(self.directory)
        path = self.directory / (identifier + ".sqlite3")
        _regular(path)
        return path

    def status(self, user, version):
        with self.source.read_library(self.account) as (_account, library):
            basis = library.revision(self.account, user)
            if basis is None:
                raise ValueError("unknown QQ conversation")
            with self.base.connect() as conn:
                row = conn.execute("SELECT published_revision,stage_revision,stage_id "
                    "FROM qq_analysis_generations_v1 WHERE account=? AND session=? AND version=?",
                    (self.account, user, version)).fetchone()
                existing = any(conn.execute(f"SELECT 1 FROM {table} WHERE account=? AND session=? "
                    f"AND {LOCAL_TABLES[table]}=? LIMIT 1", (self.account, user, version)).fetchone()
                    for table in ("results_v2", "fine_results_v1", "batch_coverage_v1"))
            return {"dataRevision": basis[0], "analysisRevision": row[0] if row else None,
                    "hasPublishedAnalysis": bool(existing or row and row[0] is not None),
                    "stale": row is None or row[0] != basis[0]}

    def job_key(self, user, version):
        with self.source.read_library(self.account) as (_account, library):
            basis = library.revision(self.account, user)
            with self.base.connect() as conn:
                row = conn.execute("SELECT stage_revision,stage_id FROM qq_analysis_generations_v1 "
                    "WHERE account=? AND session=? AND version=?", (self.account, user, version)).fetchone()
            current = row and basis and row[0] == basis[0]
            return self.account, str(self._stage_path(row[1]) if current else self.base.path), user, version

    def bind(self, user, version):
        with self.source.read_library(self.account) as (_account, library):
            basis = library.revision(self.account, user)
            if basis is None:
                raise ValueError("unknown QQ conversation")
            revision = basis[0]
            identity = self.account, user, version
            with self.base.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT published_revision,stage_revision,stage_id "
                    "FROM qq_analysis_generations_v1 WHERE account=? AND session=? AND version=?", identity).fetchone()
                published = row[0] if row else None
                existing = any(conn.execute(f"SELECT 1 FROM {table} WHERE account=? AND session=? "
                    f"AND {LOCAL_TABLES[table]}=? LIMIT 1", identity).fetchone()
                    for table in ("results_v2", "fine_results_v1", "batch_coverage_v1"))
                identifier = row[2] if row and row[1] == revision else uuid.uuid4().hex
                if row is None or row[1] != revision:
                    conn.execute("INSERT OR REPLACE INTO qq_analysis_generations_v1 VALUES (?,?,?,?,?,?)",
                                 (*identity, published, revision, identifier))
            rebuild = published != revision and (published is not None or existing)
            if identifier in self.stages:
                stage = self.stages[identifier]
                stage.requires_rebuild = rebuild
                return stage
            path = self._stage_path(identifier)
            self.directory.mkdir(parents=True, exist_ok=True)
            if not path.exists() and published == revision:
                # Recover a missing disposable working copy from committed evidence.
                with self.base.connect() as source, closing(sqlite3.connect(path)) as target:
                    source.backup(target)
            stage = QQAnalysisStage(self, path, user, version, revision, identifier, rebuild)
            BatchStateStore(stage)
            self.stages[identifier] = stage
            return stage

    def bind_labels(self,user,version,model_version):
        """Per-message group publication; portrait/whole-generation guards stay intact."""
        with self.source.read_library(self.account) as (_,library):
            revision=library.revision(self.account,user)
            if revision is None:raise ValueError('unknown QQ conversation')
            identity=(self.account,user,version)
            with self.base.connect() as conn:
                row=conn.execute('SELECT stage_id FROM qq_analysis_generations_v1 WHERE account=? AND session=? AND version=?',identity).fetchone()
                identifier=row[0] if row else uuid.uuid4().hex
                if not row:
                    conn.execute('INSERT INTO qq_analysis_generations_v1 VALUES (?,?,?,?,?,?)',(*identity,None,revision[0],identifier))
            key='labels:'+identifier
            if key not in self.stages:
                self.stages[key]=QQGroupLabelStage(self,user,version,model_version,revision[0],identifier)
            return self.stages[key]

    def publish(self, stage):
        with stage.guard(), self.base.profile_lock:
            # Each scope is replaced in one transaction. An exception preserves all
            # old rows and its published_revision; no partially new aggregate leaks.
            with self.base.connect() as conn:
                conn.execute("ATTACH DATABASE ? AS qq_stage", (str(stage.path),))
                conn.execute("BEGIN IMMEDIATE")
                for table, column in LOCAL_TABLES.items():
                    conn.execute(f"DELETE FROM {table} WHERE account=? AND session=? AND {column}=?", stage.identity)
                    conn.execute(f"INSERT INTO main.{table} SELECT * FROM qq_stage.{table} "
                        f"WHERE account=? AND session=? AND {column}=? ORDER BY rowid", stage.identity)
                # Legacy rowid cursors belong to a physical database. Preserve the
                # same prefix after copying rows among other conversations/versions.
                for table, version_column, cursor in (("batch_progress_v1", "base_version", "legacy_max_rowid"),
                                                       ("profile_state_v1", "version", "cursor")):
                    conn.execute(f"UPDATE {table} SET {cursor}=COALESCE((SELECT MAX(current.rowid) "
                        "FROM main.results_v2 current JOIN qq_stage.results_v2 previous "
                        "ON current.account=previous.account AND current.session=previous.session "
                        "AND current.version=previous.version AND current.id=previous.id "
                        f"WHERE previous.account={table}.account AND previous.session={table}.session "
                        f"AND previous.version={table}.{version_column} AND previous.rowid<={table}.{cursor}),0) "
                        f"WHERE account=? AND session=? AND {version_column}=?", stage.identity)
                conn.execute("UPDATE qq_analysis_generations_v1 SET published_revision=? "
                    "WHERE account=? AND session=? AND version=? AND stage_id=?",
                    (stage.revision, *stage.identity, stage.identifier))
            return True

    def clear_stages(self):
        # Called only after the existing account-scoped worker drain and cache clear.
        # Revoked generation rows already prevent old work from publishing.
        _check_root(self.directory)
        if self.directory.exists():
            paths = list(self.directory.iterdir())
            for path in paths:
                if not re.fullmatch(r"[0-9a-f]{32}\.sqlite3(?:-journal|-wal|-shm)?", path.name) or not _regular(path):
                    raise RuntimeError("unexpected file in QQ analysis staging directory")
            for path in paths:
                path.unlink()
        self.stages.clear()


class StaleGroupInputError(StaleAnalysisError):
    """Only this message's inference is obsolete; other labels can still publish."""


class QQGroupLabelStage(ResultStore):
    def __init__(self,owner,user,version,model_version,revision,identifier):
        self.owner,self.user,self.version=owner,user,version
        self.model_version,self.revision,self.identifier=model_version,revision,identifier
        self.requires_rebuild=False
        self.control_epoch=owner.base.analysis_epoch(owner.account,user)
        super().__init__(owner.base.path,transaction_guard=self.guard)
        self.profile_lock=owner.base.profile_lock
        self.scope=AnalysisScope(self)

    @contextmanager
    def guard(self):
        # Same lock order as ingestion/read DTOs. No lock spans a model call.
        with self.owner.source.read_library(self.owner.account) as (_,library),self.owner.base.profile_lock:
            from analysis_control import require_running
            require_running(self.owner.base,self.owner.account,self.user,self.control_epoch)
            if library.revision(self.owner.account,self.user) is None:
                raise StaleAnalysisError()
            with self.owner.base.connect() as conn:
                row=conn.execute('SELECT stage_id FROM qq_analysis_generations_v1 WHERE account=? AND session=? AND version=?',
                    (self.owner.account,self.user,self.version)).fetchone()
            if row!=(self.identifier,) or self.cache_suspended(self.owner.account,LOCAL_SOURCE_ID):
                raise StaleAnalysisError()
            yield

    def cache_suspended(self,account,source_id):
        return self.owner.base.cache_suspended(account,source_id)

    def save_fine(self,account,user,version,message,result):
        from qq_group_analysis import current_basis
        if (account,user,version)!=(self.owner.account,self.user,self.version):
            raise StaleAnalysisError()
        with self.guard():
            basis=current_basis(self.owner.source,account,user,self.model_version,message['id'])
            if basis is None or basis!=result.get('qqInputBasis'):
                raise StaleGroupInputError()
            with self.owner.source.read_library(account) as (_,library):
                revision=library.revision(account,user)[0]
            result={**result,'qqInputRevision':revision}
            # Commit one input-verified result and its publication marker together.
            seq,shard,local_id=message['_sort']
            import json
            with self.connect() as conn:
                conn.execute('INSERT INTO fine_results_v1 VALUES (?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(account,session,id,version) DO UPDATE SET result=excluded.result',
                    (account,user,message['id'],version,seq,shard,local_id,message['senderId'],json.dumps(result,ensure_ascii=False)))
                conn.execute('DELETE FROM fine_skips_v1 WHERE account=? AND session=? AND id=? AND version=?',
                    (account,user,message['id'],version))
                conn.execute('UPDATE qq_analysis_generations_v1 SET published_revision=?,stage_revision=? '
                    'WHERE account=? AND session=? AND version=? AND stage_id=?',
                    (revision,revision,account,user,version,self.identifier))
                self.complete_resume_target(conn,account,user,version,message['id'])
            self.revision=revision

    def skip_fine(self,account,user,version,message,reason):
        from backend_contracts import FINE_LABEL_SCHEMA
        return self.save_fine(account,user,version,message,{'state':'skipped','reason':reason,
            'labelSchema':FINE_LABEL_SCHEMA,'qqInputBasis':message.get('_qqInputBasis')})

    def publish(self,*,complete=False):
        with self.guard():
            return True

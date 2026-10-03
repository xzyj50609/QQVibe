"""Account-scoped SQLite analysis repository.

Owns schema upgrades, transactions, results, progress, and persisted API caches.
Services call repository methods and never issue SQL through this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager, nullcontext

from backend_contracts import (
    FINE_LABEL_SCHEMA, LOCAL_SOURCE_ID, ROOT, empty_api_portrait, valid_api_portrait,
)
from profile_signals import keyword_counts
from profile_state import empty_state as empty_profile_state, add_result as add_profile_result
from product_profile import current_product

PRODUCT = current_product()


def project_result_store(account, _workdir):
    """Keep the established account-hashed cache location, independent of snapshots."""
    directory = PRODUCT.state_dir("real-client-data", ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    filename = hashlib.sha256(account.encode("utf-8")).hexdigest() + ".sqlite3"
    return ResultStore(directory / filename)


class ResultStore:
    def __init__(self, path, *, transaction_guard=None):
        self.path = path
        self.transaction_guard = transaction_guard or nullcontext
        self.profile_lock = threading.RLock()
        with self.connect() as conn:
            # Serialize first-time schema creation and nullable-column migration.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS qq_analysis_control_v1 (account TEXT NOT NULL,session TEXT NOT NULL,"
                         "state TEXT NOT NULL CHECK(state IN ('running','paused','cancelled')),PRIMARY KEY(account,session))")
            conn.execute("CREATE TABLE IF NOT EXISTS qq_analysis_resume_v1 (account TEXT NOT NULL,session TEXT NOT NULL,"
                         "version TEXT NOT NULL,targets TEXT NOT NULL,range_limit INTEGER NOT NULL,PRIMARY KEY(account,session,version))")
            if 'epoch' not in {row[1] for row in conn.execute('PRAGMA table_info(qq_analysis_control_v1)')}:
                conn.execute('ALTER TABLE qq_analysis_control_v1 ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0')
            conn.execute('CREATE TABLE IF NOT EXISTS qq_api_request_budget_v1 (account TEXT NOT NULL,session TEXT NOT NULL,'
                'source_id TEXT NOT NULL,request_limit INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0,'
                'PRIMARY KEY(account,session,source_id),CHECK(request_limit>=0 AND used>=0 AND used<=request_limit))')
            conn.execute("CREATE TABLE IF NOT EXISTS results_v2 (account TEXT NOT NULL, session TEXT NOT NULL, id TEXT NOT NULL, "
                         "sort_seq INTEGER NOT NULL, shard TEXT NOT NULL, local_id INTEGER NOT NULL, sender TEXT NOT NULL, "
                         "side TEXT NOT NULL, result TEXT NOT NULL, score REAL, version TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,id,version))")
            conn.execute("CREATE TABLE IF NOT EXISTS analysis_skips (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "id TEXT NOT NULL, version TEXT NOT NULL, sort_seq INTEGER NOT NULL, shard TEXT NOT NULL, "
                         "local_id INTEGER NOT NULL, reason TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,id,version))")
            conn.execute("CREATE TABLE IF NOT EXISTS fine_results_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "id TEXT NOT NULL, version TEXT NOT NULL, sort_seq INTEGER NOT NULL, shard TEXT NOT NULL, "
                         "local_id INTEGER NOT NULL, sender TEXT NOT NULL, result TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,id,version))")
            conn.execute("CREATE TABLE IF NOT EXISTS fine_skips_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "id TEXT NOT NULL, version TEXT NOT NULL, sort_seq INTEGER NOT NULL, shard TEXT NOT NULL, "
                         "local_id INTEGER NOT NULL, reason TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,id,version))")
            conn.execute("CREATE INDEX IF NOT EXISTS fine_results_recent_v1 ON fine_results_v1 "
                         "(account,session,version,sort_seq DESC,shard DESC,local_id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS fine_skips_recent_v1 ON fine_skips_v1 "
                         "(account,session,version,sort_seq DESC,shard DESC,local_id DESC)")
            conn.execute("CREATE TABLE IF NOT EXISTS api_insights_v1 (account TEXT NOT NULL, "
                         "session TEXT NOT NULL, source_id TEXT NOT NULL, id TEXT NOT NULL, "
                         "sort_seq INTEGER NOT NULL, shard TEXT NOT NULL, local_id INTEGER NOT NULL, "
                         "result TEXT NOT NULL, PRIMARY KEY(account,session,source_id,id))")
            conn.execute("CREATE INDEX IF NOT EXISTS api_insights_recent_v1 ON api_insights_v1 "
                         "(account,session,source_id,sort_seq DESC,shard DESC,local_id DESC)")
            conn.execute("CREATE TABLE IF NOT EXISTS api_portrait_v1 (account TEXT NOT NULL, "
                         "session TEXT NOT NULL, source_id TEXT NOT NULL, subject TEXT NOT NULL, "
                         "highwater_json TEXT, after_json TEXT, fingerprint TEXT NOT NULL, "
                         "available_json TEXT NOT NULL, "
                         "plan_json TEXT NOT NULL, batch_index INTEGER NOT NULL, "
                         "complete INTEGER NOT NULL, processed INTEGER NOT NULL, "
                         "processed_chars INTEGER NOT NULL, portrait_json TEXT NOT NULL, "
                         "resume_json TEXT, "
                         "PRIMARY KEY(account,session,source_id,subject))")
            if "resume_json" not in {row[1] for row in conn.execute("PRAGMA table_info(api_portrait_v1)")}:
                try:
                    conn.execute("ALTER TABLE api_portrait_v1 ADD COLUMN resume_json TEXT")
                except sqlite3.OperationalError:
                    # Concurrent first opens can both observe the old schema. Only
                    # accept the race if the other writer installed this column.
                    if "resume_json" not in {row[1] for row in
                                             conn.execute("PRAGMA table_info(api_portrait_v1)")}:
                        raise
            conn.execute("CREATE TABLE IF NOT EXISTS api_history_inventory_v1 (account TEXT NOT NULL, "
                         "session TEXT NOT NULL, subject TEXT NOT NULL, highwater_json TEXT, "
                         "fingerprint TEXT NOT NULL, available_json TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,subject))")
            conn.execute("CREATE TABLE IF NOT EXISTS api_source_meta_v1 (account TEXT NOT NULL, "
                         "source_id TEXT NOT NULL, protocol TEXT NOT NULL, model TEXT NOT NULL, "
                         "PRIMARY KEY(account,source_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS analysis_cache_suspended_v1 (account TEXT NOT NULL, "
                         "source_id TEXT NOT NULL, PRIMARY KEY(account,source_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS progress_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "version TEXT NOT NULL, cursor_seq INTEGER, cursor_shard TEXT, cursor_local INTEGER, "
                         "complete INTEGER NOT NULL DEFAULT 0, context_json TEXT NOT NULL DEFAULT '[]', "
                         "eligible_count INTEGER NOT NULL DEFAULT 0, score_count INTEGER NOT NULL DEFAULT 0, "
                         "score_sum REAL NOT NULL DEFAULT 0, score_weighted REAL NOT NULL DEFAULT 0, "
                         "mood_count INTEGER NOT NULL DEFAULT 0, mood_json TEXT NOT NULL DEFAULT '{}', "
                         "PRIMARY KEY(account,session,version))")
            conn.execute("CREATE TABLE IF NOT EXISTS summary_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "version TEXT NOT NULL, score_count INTEGER NOT NULL, score_sum REAL NOT NULL, "
                         "score_weighted REAL NOT NULL, mood_count INTEGER NOT NULL, mood_json TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,version))")
            conn.execute("CREATE INDEX IF NOT EXISTS results_order_v1 ON results_v2 "
                         "(account,session,version,sort_seq,shard,local_id,id)")
            conn.execute("CREATE INDEX IF NOT EXISTS results_scored_order_v1 ON results_v2 "
                         "(account,session,version,sort_seq,shard,local_id,id) "
                         "WHERE side='other' AND score IS NOT NULL")
            conn.execute("CREATE TABLE IF NOT EXISTS profile_tokens_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "version TEXT NOT NULL, id TEXT NOT NULL, words TEXT NOT NULL, PRIMARY KEY(account,session,version,id))")
            conn.execute("CREATE TABLE IF NOT EXISTS profile_tokens_ready_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "version TEXT NOT NULL, PRIMARY KEY(account,session,version))")
            conn.execute("CREATE TABLE IF NOT EXISTS profile_state_v1 (account TEXT NOT NULL, session TEXT NOT NULL, "
                         "version TEXT NOT NULL, subject TEXT NOT NULL, cursor INTEGER NOT NULL, state TEXT NOT NULL, "
                         "PRIMARY KEY(account,session,version,subject))")
            conn.execute("CREATE INDEX IF NOT EXISTS results_profile_delta_v1 ON results_v2 (account,session,version)")
            conn.execute("CREATE INDEX IF NOT EXISTS results_member_delta_v1 ON results_v2 (account,session,version,sender)")
            conn.execute("CREATE TABLE IF NOT EXISTS qq_analysis_generations_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,version TEXT NOT NULL,"
                         "published_revision INTEGER,stage_revision INTEGER NOT NULL,stage_id TEXT NOT NULL,"
                         "PRIMARY KEY(account,session,version))")
            conn.execute("CREATE TABLE IF NOT EXISTS qq_api_generations_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,source_id TEXT NOT NULL,"
                         "component TEXT NOT NULL,subject TEXT NOT NULL,published_revision INTEGER,"
                         "stage_revision INTEGER NOT NULL,stage_id TEXT NOT NULL,"
                         "PRIMARY KEY(account,session,source_id,component,subject))")
            conn.execute("CREATE TABLE IF NOT EXISTS qq_api_insight_basis_v1 ("
                         "account TEXT NOT NULL,session TEXT NOT NULL,source_id TEXT NOT NULL,id TEXT NOT NULL,"
                         "revision INTEGER NOT NULL,context_hash TEXT NOT NULL,"
                         "PRIMARY KEY(account,session,source_id,id))")

    @contextmanager
    def connect(self):
        with self.transaction_guard():
            conn = sqlite3.connect(self.path, timeout=15)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def analysis_state(self,account,user):
        with self.connect() as conn:
            row=conn.execute('SELECT state FROM qq_analysis_control_v1 WHERE account=? AND session=?',(account,user)).fetchone()
        return row[0] if row else 'running'

    def analysis_epoch(self,account,user):
        with self.connect() as conn:
            row=conn.execute('SELECT epoch FROM qq_analysis_control_v1 WHERE account=? AND session=?',(account,user)).fetchone()
        return row[0] if row else 0

    def api_request_budget(self,account,user,source_id):
        with self.connect() as conn:
            row=conn.execute('SELECT request_limit,used FROM qq_api_request_budget_v1 WHERE account=? AND session=? AND source_id=?',
                (account,user,source_id)).fetchone()
        limit,used=row if row else (24,0)
        return {'requestLimit':limit,'usedRequests':used,'remainingRequests':limit-used,'includesRetries':True,'currencyCost':None}

    def grant_api_requests(self,account,user,source_id,requests):
        if type(requests) is not int or not 1<=requests<=1000:raise ValueError('invalid-request-budget')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO qq_api_request_budget_v1 VALUES (?,?,?,24,0)',(account,user,source_id))
            conn.execute('UPDATE qq_api_request_budget_v1 SET request_limit=used+? WHERE account=? AND session=? AND source_id=?',
                (requests,account,user,source_id))
        return self.api_request_budget(account,user,source_id)

    def reserve_api_request(self,account,user,source_id):
        from analysis_control import AnalysisBudgetExhausted
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO qq_api_request_budget_v1 VALUES (?,?,?,24,0)',(account,user,source_id))
            cursor=conn.execute('UPDATE qq_api_request_budget_v1 SET used=used+1 WHERE account=? AND session=? AND source_id=? AND used<request_limit',
                (account,user,source_id))
            if cursor.rowcount!=1:raise AnalysisBudgetExhausted()
        return self.api_request_budget(account,user,source_id)

    def set_analysis_state(self,account,user,state):
        if state not in ('running','paused','cancelled'):raise ValueError('invalid-analysis-state')
        with self.connect() as conn:
            conn.execute('INSERT INTO qq_analysis_control_v1 (account,session,state,epoch) VALUES (?,?,?,?) '
                'ON CONFLICT(account,session) DO UPDATE SET state=excluded.state,epoch=epoch+excluded.epoch',
                (account,user,state,int(state!='running')))
            if state=='cancelled':conn.execute('DELETE FROM qq_analysis_resume_v1 WHERE account=? AND session=?',(account,user))

    def resume_targets(self,account,user,version):
        with self.connect() as conn:
            row=conn.execute('SELECT targets,range_limit FROM qq_analysis_resume_v1 WHERE account=? AND session=? AND version=?',
                (account,user,version)).fetchone()
        return (json.loads(row[0]),row[1]) if row else ([],0)

    def add_resume_targets(self,account,user,version,ids,limit):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT targets,range_limit FROM qq_analysis_resume_v1 WHERE account=? AND session=? AND version=?',
                (account,user,version)).fetchone()
            targets=list(dict.fromkeys([*(json.loads(row[0]) if row else []),*ids]))
            if len(targets)>2048:raise ValueError('group-analysis-backlog-full')
            conn.execute('INSERT OR REPLACE INTO qq_analysis_resume_v1 VALUES (?,?,?,?,?)',
                (account,user,version,json.dumps(targets),max(limit,row[1] if row else 0)))

    def complete_resume_targets(self, account, user, version, stable_ids):
        """Complete a scoped batch inside the repository's own transaction."""
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for stable_id in dict.fromkeys(stable_ids):
                self.complete_resume_target(conn, account, user, version, stable_id)

    @staticmethod
    def complete_resume_target(conn,account,user,version,stable_id):
        row=conn.execute('SELECT targets FROM qq_analysis_resume_v1 WHERE account=? AND session=? AND version=?',
            (account,user,version)).fetchone()
        if row:
            targets=[value for value in json.loads(row[0]) if value!=stable_id]
            conn.execute('UPDATE qq_analysis_resume_v1 SET targets=? WHERE account=? AND session=? AND version=?',
                (json.dumps(targets),account,user,version))

    def items(self, account, user, version, member=None):
        with self.connect() as conn:
            sql = "SELECT id,result,score,side FROM results_v2 WHERE account=? AND session=? AND version=?"
            args = [account, user, version]
            if member:
                sql += " AND sender=?"
                args.append(member)
            sql += " ORDER BY sort_seq,shard,local_id,id"
            return [(id, json.loads(result), score, side) for id, result, score, side in conn.execute(sql, args)]

    def profile_state(self, account, user, version, subject, read_texts):
        """Bootstrap existing evidence once, then consume only inserted result rows."""
        scope = (account, user, version)
        with self.profile_lock:
            with self.connect() as conn:
                ready = conn.execute("SELECT 1 FROM profile_tokens_ready_v1 WHERE account=? AND session=? AND version=?", scope).fetchone()
                refs = [] if ready else list(conn.execute(
                    "SELECT r.shard,r.local_id,r.id FROM results_v2 r LEFT JOIN profile_tokens_v1 t "
                    "ON t.account=r.account AND t.session=r.session AND t.version=r.version AND t.id=r.id "
                    "WHERE r.account=? AND r.session=? AND r.version=? AND r.side='other' AND t.id IS NULL", scope))
            if not ready:
                # Only legacy results lack stored lexical evidence. New results save it
                # together with the model outcome, including before the first profile.
                texts = read_texts(refs) if refs else {}
                tokens = {stable_id: dict(keyword_counts([texts.get(stable_id, "")]))
                          for _shard, _local, stable_id in refs}
                with self.connect() as conn:
                    conn.executemany("INSERT OR IGNORE INTO profile_tokens_v1 VALUES (?,?,?,?,?)",
                                     [(*scope, stable_id, json.dumps(words, ensure_ascii=False))
                                      for stable_id, words in tokens.items()])
                    conn.execute("INSERT OR IGNORE INTO profile_tokens_ready_v1 VALUES (?,?,?)", scope)
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                saved = conn.execute("SELECT cursor,state FROM profile_state_v1 WHERE account=? AND session=? AND version=? AND subject=?",
                                     (*scope, subject or "")).fetchone()
                cursor, state = (saved[0], json.loads(saved[1])) if saved else (0, empty_profile_state())
                conditions = "r.account=? AND r.session=? AND r.version=? AND r.rowid>?"
                args = (*scope, cursor)
                if subject:
                    conditions += " AND r.sender=?"
                    args += (subject,)
                ordered = "r.rowid" if saved else "r.sort_seq,r.shard,r.local_id,r.id"
                rows = conn.execute(
                    "SELECT r.rowid,r.id,r.result,r.score,r.side,r.sort_seq,r.shard,r.local_id,t.words "
                    "FROM results_v2 r LEFT JOIN profile_tokens_v1 t ON t.account=r.account AND t.session=r.session "
                    "AND t.version=r.version AND t.id=r.id WHERE " + conditions + " ORDER BY " + ordered, args).fetchall()
                for rowid, stable_id, raw, score, side, seq, shard, local_id, words in rows:
                    result, position = json.loads(raw), (seq, shard, local_id, stable_id)
                    tails = []
                    if saved and side == "other" and state["latest"] is not None and position < tuple(state["latest"]):
                        tail_where = "account=? AND session=? AND version=? AND side='other' AND rowid<? AND (sort_seq,shard,local_id,id)>(?,?,?,?)"
                        tail_args = (*scope, rowid, *position)
                        if subject:
                            tail_where += " AND sender=?"
                            tail_args += (subject,)
                        tails = [(json.loads(raw_tail), tail_score) for raw_tail, tail_score in
                                 conn.execute("SELECT result,score FROM results_v2 WHERE " + tail_where, tail_args)]
                    add_profile_result(state, result, score, side, position, json.loads(words or "{}"), tails)
                    cursor = max(cursor, rowid)
                if rows or not saved:
                    conn.execute("INSERT OR REPLACE INTO profile_state_v1 VALUES (?,?,?,?,?,?)",
                                 (*scope, subject or "", cursor, json.dumps(state, ensure_ascii=False)))
                return state

    def ids(self, account, user, version):
        with self.connect() as conn:
            found = {row[0] for row in conn.execute(
                "SELECT id FROM results_v2 WHERE account=? AND session=? AND version=?",
                (account, user, version),
            )}
            found.update(row[0] for row in conn.execute(
                "SELECT id FROM analysis_skips WHERE account=? AND session=? AND version=?",
                (account, user, version),
            ))
            return found

    def has(self, account, user, version, stable_id):
        with self.connect() as conn:
            return (conn.execute("SELECT 1 FROM results_v2 WHERE account=? AND session=? AND id=? AND version=?",
                                 (account, user, stable_id, version)).fetchone() is not None or
                    conn.execute("SELECT 1 FROM analysis_skips WHERE account=? AND session=? AND id=? AND version=?",
                                 (account, user, stable_id, version)).fetchone() is not None)

    def skip(self, account, user, version, message, reason):
        seq, shard, local_id = message["_sort"]
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO analysis_skips VALUES (?,?,?,?,?,?,?,?)",
                         (account, user, message["id"], version, seq, shard, local_id, reason))

    def first_skipped_position(self, account, user, version):
        """Return the earliest legacy skip so the service can schedule a rescan."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT sort_seq,shard,local_id FROM analysis_skips "
                "WHERE account=? AND session=? AND version=? "
                "ORDER BY sort_seq,shard,local_id LIMIT 1",
                (account, user, version),
            ).fetchone()

    def progress(self, account, user, version):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cursor_seq,cursor_shard,cursor_local,complete,context_json,eligible_count FROM progress_v1 "
                "WHERE account=? AND session=? AND version=?", (account, user, version),
            ).fetchone()
        if row is None:
            return {"cursor": None, "complete": False, "context": [], "eligible": 0}
        return {"cursor": tuple(row[:3]) if row[0] is not None else None,
                "complete": bool(row[3]), "context": json.loads(row[4]), "eligible": row[5]}

    def advance(self, account, user, version, cursor, context, item=None, complete=False):
        """Commit only the scan cursor; result summaries are updated on first save."""
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO progress_v1(account,session,version) VALUES (?,?,?)",
                         (account, user, version))
            row = conn.execute(
                "SELECT cursor_seq,cursor_shard,cursor_local,eligible_count FROM progress_v1 "
                "WHERE account=? AND session=? AND version=?", (account, user, version),
            ).fetchone()
            prior = tuple(row[:3]) if row[0] is not None else None
            if cursor is not None and prior is not None and (cursor < prior or
                    (cursor == prior and not complete)):
                return
            eligible = row[3]
            if item is not None and item["kind"] == "text" and item["text"].strip():
                eligible += 1
            seq, shard, local_id = cursor if cursor is not None else (None, None, None)
            conn.execute("UPDATE progress_v1 SET cursor_seq=?,cursor_shard=?,cursor_local=?,complete=?,"
                         "context_json=?,eligible_count=? WHERE account=? AND session=? AND version=?",
                         (seq, shard, local_id, int(complete),
                          json.dumps(list(context), ensure_ascii=False), eligible, account, user, version))

    def recent(self, account, user, version, limit=80):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,result,score,side FROM results_v2 WHERE account=? AND session=? AND version=? "
                "ORDER BY sort_seq DESC,shard DESC,local_id DESC,id DESC LIMIT ?",
                (account, user, version, limit),
            ).fetchall()
            skipped = conn.execute(
                "SELECT id,reason FROM analysis_skips WHERE account=? AND session=? AND version=? "
                "ORDER BY sort_seq DESC,shard DESC,local_id DESC,id DESC LIMIT ?",
                (account, user, version, limit),
            ).fetchall()
        return ({stable_id: json.loads(result) for stable_id, result, _score, _side in rows} |
                {stable_id: {"state": "skipped", "reason": reason} for stable_id, reason in skipped})

    def fine_known(self, account, user, version, ids):
        if not ids:
            return set()
        if len(ids) > 500:
            raise ValueError("too many fine result ids")
        marks = ",".join("?" for _ in ids)
        with self.connect() as conn:
            scope = (account, user, version, *ids)
            main = {stable_id: json.loads(raw).get("labelSchema") for stable_id, raw in conn.execute(
                f"SELECT id,result FROM results_v2 WHERE account=? AND session=? AND version=? AND id IN ({marks})",
                scope)}
            fine = {stable_id: json.loads(raw).get("labelSchema") for stable_id, raw in conn.execute(
                f"SELECT id,result FROM fine_results_v1 WHERE account=? AND session=? AND version=? AND id IN ({marks})",
                scope)}
            # A legacy fine row overlays a newer main row in fine_view, so it
            # must be refreshed too. Only the caller's visible ids are queried.
            known = {stable_id for stable_id, schema in fine.items() if schema == FINE_LABEL_SCHEMA}
            known.update(stable_id for stable_id, schema in main.items()
                         if schema == FINE_LABEL_SCHEMA and stable_id not in fine)
            # A recorded skip is terminal for this analysis version. In particular,
            # retrying a legacy fine row that already hit the length limit would
            # repeat the same failed model request on every visit.
            for table in ("analysis_skips", "fine_skips_v1"):
                known.update(row[0] for row in conn.execute(
                    f"SELECT id FROM {table} WHERE account=? AND session=? AND version=? AND id IN ({marks})",
                    scope))
            return known

    def fine_view(self, account, user, version, ids=None, limit=80):
        if ids is not None and not ids:
            return {}
        if ids is not None and len(ids) > 500:
            raise ValueError("too many fine result ids")
        where = "account=? AND session=? AND version=?"
        args = [account, user, version]
        if ids is not None:
            where += " AND id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        order = "" if ids is not None else " ORDER BY sort_seq DESC,shard DESC,local_id DESC,id DESC LIMIT ?"
        if ids is None:
            args.append(limit)
        with self.connect() as conn:
            done = conn.execute("SELECT id,result FROM fine_results_v1 WHERE " + where + order, args).fetchall()
            skipped = conn.execute("SELECT id,reason FROM fine_skips_v1 WHERE " + where + order, args).fetchall()
        return ({stable_id: json.loads(raw) for stable_id, raw in done} |
                {stable_id: {"state": "skipped", "reason": reason} for stable_id, reason in skipped})

    def save_fine(self, account, user, version, message, result):
        seq, shard, local_id = message["_sort"]
        with self.connect() as conn:
            conn.execute("INSERT INTO fine_results_v1 VALUES (?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(account,session,id,version) DO UPDATE SET result=excluded.result",
                         (account, user, message["id"], version, seq, shard, local_id,
                          message["senderId"], json.dumps(result, ensure_ascii=False)))
            conn.execute("DELETE FROM fine_skips_v1 WHERE account=? AND session=? AND id=? AND version=?",
                         (account, user, message["id"], version))

    def api_insight_known(self, account, user, source_id, ids):
        if not ids:
            return set()
        if len(ids) > 500:
            raise ValueError("too many API insight ids")
        marks = ",".join("?" for _ in ids)
        with self.connect() as conn:
            return {row[0] for row in conn.execute(
                f"SELECT id FROM api_insights_v1 WHERE account=? AND session=? AND source_id=? AND id IN ({marks})",
                (account, user, source_id, *ids))}

    def api_insight_view(self, account, user, source_id, ids=None, limit=500):
        if ids is not None and not ids:
            return {}
        if ids is not None and len(ids) > 500:
            raise ValueError("too many API insight ids")
        where = "account=? AND session=? AND source_id=?"
        args = [account, user, source_id]
        if ids is not None:
            where += " AND id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        else:
            where += " ORDER BY sort_seq DESC,shard DESC,local_id DESC,id DESC LIMIT ?"
            args.append(limit)
        with self.connect() as conn:
            return {stable_id: json.loads(raw) for stable_id, raw in conn.execute(
                "SELECT id,result FROM api_insights_v1 WHERE " + where, args)}

    def save_api_insights(self, account, user, source_id, rows, *, basis=None):
        with self.connect() as conn:
            for message, insight in rows:
                seq, shard, local_id = message["_sort"]
                conn.execute("INSERT INTO api_insights_v1 VALUES (?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(account,session,source_id,id) DO UPDATE SET result=excluded.result",
                              (account, user, source_id, message["id"], seq, shard, local_id,
                               json.dumps(insight, ensure_ascii=False)))
                if basis is not None:
                    conn.execute("INSERT OR REPLACE INTO qq_api_insight_basis_v1 VALUES (?,?,?,?,?,?)",
                                 (account, user, source_id, message["id"], *basis))

    def api_insight_basis_ids(self, account, user, source_id, ids, revision, context_hash):
        if not ids:
            return set()
        if len(ids) > 500:
            raise ValueError("too many API insight ids")
        marks = ",".join("?" for _ in ids)
        with self.connect() as conn:
            return {row[0] for row in conn.execute(
                "SELECT id FROM qq_api_insight_basis_v1 WHERE account=? AND session=? AND source_id=? "
                f"AND revision=? AND context_hash=? AND id IN ({marks})",
                (account, user, source_id, revision, context_hash, *ids))}

    def register_api_source(self, account, source_id, protocol, model):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO api_source_meta_v1 VALUES (?,?,?,?)",
                         (account, source_id, protocol, model))

    def cache_suspended(self, account, source_id):
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM analysis_cache_suspended_v1 WHERE account=? AND source_id=?",
                                (account, source_id)).fetchone() is not None

    def suspend_cache(self, account, source_id):
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO analysis_cache_suspended_v1 VALUES (?,?)",
                         (account, source_id))

    def resume_cache(self, account, source_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM analysis_cache_suspended_v1 WHERE account=? AND source_id=?",
                         (account, source_id))

    def clear_legacy_suspensions(self, account):
        """A suspension only ever lived for the duration of a Clear, so one found at
        rest is a leftover from the previous Clear behavior and must not block analysis."""
        with self.connect() as conn:
            conn.execute("DELETE FROM analysis_cache_suspended_v1 WHERE account=?", (account,))

    def api_history_inventory_get(self, account, user, subject, highwater):
        with self.connect() as conn:
            row = conn.execute("SELECT highwater_json,fingerprint,available_json "
                               "FROM api_history_inventory_v1 WHERE account=? AND session=? AND subject=?",
                               (account, user, subject)).fetchone()
        if row is None or (tuple(json.loads(row[0])) if row[0] else None) != highwater:
            return None
        return {"fingerprint": row[1], "available": json.loads(row[2])}

    def api_history_inventory_save(self, account, user, subject, highwater, fingerprint, available):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO api_history_inventory_v1 VALUES (?,?,?,?,?,?)",
                         (account, user, subject, json.dumps(highwater) if highwater else None,
                          fingerprint, json.dumps(available, ensure_ascii=False)))

    def api_portrait_get(self, account, user, source_id, subject):
        with self.connect() as conn:
            row = conn.execute("SELECT highwater_json,after_json,fingerprint,available_json,plan_json,"
                               "batch_index,complete,processed,processed_chars,portrait_json,resume_json "
                               "FROM api_portrait_v1 WHERE account=? AND session=? AND source_id=? AND subject=?",
                               (account, user, source_id, subject)).fetchone()
        if row is None:
            return None
        portrait = json.loads(row[9])
        if not valid_api_portrait(portrait):
            raise RuntimeError("invalid saved API portrait")
        return {"highwater": tuple(json.loads(row[0])) if row[0] else None,
                "after": tuple(json.loads(row[1])) if row[1] else None,
                "fingerprint": row[2], "available": json.loads(row[3]),
                "plan": json.loads(row[4]), "batchIndex": row[5],
                "complete": bool(row[6]), "processed": row[7],
                "processedChars": row[8], "portrait": portrait,
                "resume": json.loads(row[10]) if row[10] else None}

    def api_portrait_begin(self, account, user, source_id, subject, highwater, after,
                           fingerprint, available, plan):
        saved = self.api_portrait_get(account, user, source_id, subject)
        if saved and not saved["complete"]:
            if (saved["highwater"] != highwater or saved["after"] != after or
                    saved["fingerprint"] != fingerprint):
                raise ValueError("unfinished portrait source changed")
            if saved["plan"] != plan:
                completed = saved["batchIndex"]
                if saved["plan"][:completed] != plan[:completed]:
                    raise ValueError("finished portrait batches cannot be replanned")
                with self.connect() as conn:
                    changed = conn.execute(
                        "UPDATE api_portrait_v1 SET plan_json=? WHERE account=? AND session=? "
                        "AND source_id=? AND subject=? AND batch_index=? AND complete=0 AND plan_json=?",
                        (json.dumps(plan), account, user, source_id, subject, completed,
                         json.dumps(saved["plan"]))).rowcount
                    if changed != 1:
                        raise RuntimeError("API portrait plan changed concurrently")
                saved["plan"] = plan
            return saved
        if saved and (highwater is None or saved["highwater"] == highwater):
            return saved
        portrait = saved["portrait"] if saved else empty_api_portrait()
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO api_portrait_v1 "
                         "(account,session,source_id,subject,highwater_json,after_json,fingerprint,"
                         "available_json,plan_json,batch_index,complete,processed,processed_chars,"
                         "portrait_json,resume_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (account, user, source_id, subject,
                          json.dumps(highwater) if highwater else None,
                          json.dumps(after) if after else None, fingerprint,
                          json.dumps(available, ensure_ascii=False), json.dumps(plan), 0,
                          int(not plan), 0, 0, json.dumps(portrait, ensure_ascii=False), None))
        return self.api_portrait_get(account, user, source_id, subject)

    def api_portrait_upgrade_resume(self, account, user, source_id, subject,
                                    fingerprint, batch_index, available, resume):
        """Attach a verified seek point to one legacy checkpoint without changing it."""
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE api_portrait_v1 SET available_json=?,resume_json=? WHERE account=? AND "
                "session=? AND source_id=? AND subject=? AND fingerprint=? AND batch_index=? "
                "AND complete=0",
                (json.dumps(available, ensure_ascii=False), json.dumps(resume), account, user,
                 source_id, subject, fingerprint, batch_index)).rowcount
            if changed != 1:
                raise RuntimeError("API portrait checkpoint changed during resume upgrade")

    def api_portrait_checkpoint(self, account, user, source_id, subject, batch_index,
                                portrait, processed, processed_chars, complete,
                                processed_target=None, resume=None):
        if (not valid_api_portrait(portrait) or type(processed) is not int or processed < 0 or
                type(processed_chars) is not int or processed_chars < 0 or
                processed_target is not None and (type(processed_target) is not int or processed_target < 0) or
                resume is not None and (not isinstance(resume, dict) or
                                        resume.get("batchIndex") != batch_index)):
            raise ValueError("invalid API portrait checkpoint")
        with self.connect() as conn:
            row = conn.execute("SELECT available_json FROM api_portrait_v1 WHERE account=? AND session=? "
                               "AND source_id=? AND subject=?",
                               (account, user, source_id, subject)).fetchone()
            if row is None:
                raise RuntimeError("API portrait scope disappeared")
            available = json.loads(row[0])
            if processed_target is not None:
                if processed_target > available.get("targetTextCount", processed_target):
                    raise ValueError("invalid API target progress")
                available["processedTargetTextCount"] = processed_target
            resume_clause = ",resume_json=?" if resume is not None else ""
            args = (batch_index, int(complete), processed, processed_chars,
                    json.dumps(portrait, ensure_ascii=False), json.dumps(available, ensure_ascii=False))
            if resume is not None:
                args += (json.dumps(resume),)
            changed = conn.execute("UPDATE api_portrait_v1 SET batch_index=?,complete=?,processed=?,"
                                   "processed_chars=?,portrait_json=?,available_json=?" + resume_clause +
                                   " WHERE account=? AND session=? AND source_id=? AND subject=?",
                                   (*args, account, user, source_id, subject)).rowcount
            if changed != 1:
                raise RuntimeError("API portrait scope disappeared")

    def analysis_cache_sources(self, account):
        with self.connect() as conn:
            present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            message_parts = [f"SELECT session,{column} AS id FROM {table} WHERE account=?"
                             for table, column in (("results_v2", "id"), ("fine_results_v1", "id"),
                                                   ("batch_coverage_v1", "message_id"))
                             if table in present]
            local_messages = conn.execute("SELECT COUNT(*) FROM (" + " UNION ".join(message_parts) + ")",
                                          (account,) * len(message_parts)).fetchone()[0]
            portrait_parts = [f"SELECT session,{column} AS subject FROM {table} WHERE account=?"
                              for table, column in (("profile_state_v1", "subject"),
                                                    ("summary_v1", "session"),
                                                    ("batch_progress_v1", "subject"))
                              if table in present]
            local_portraits = conn.execute("SELECT COUNT(*) FROM (" + " UNION ".join(portrait_parts) + ")",
                                           (account,) * len(portrait_parts)).fetchone()[0]
            sources = [{"sourceId": LOCAL_SOURCE_ID, "kind": "local", "label": "本地 Laya",
                        "messageCount": local_messages, "portraitCount": local_portraits,
                        "suspended": conn.execute("SELECT 1 FROM analysis_cache_suspended_v1 "
                                                  "WHERE account=? AND source_id=?",
                                                  (account, LOCAL_SOURCE_ID)).fetchone() is not None}]
            ids = {row[0] for row in conn.execute("SELECT source_id FROM api_source_meta_v1 WHERE account=?", (account,))}
            for table in ("api_insights_v1", "api_portrait_v1"):
                ids.update(raw.split(":", 1)[0] for (raw,) in conn.execute(
                    f"SELECT DISTINCT source_id FROM {table} WHERE account=?", (account,)))
            ids.update(row[0] for row in conn.execute("SELECT source_id FROM analysis_cache_suspended_v1 "
                                                       "WHERE account=?", (account,)))
            for source_id in sorted(ids):
                if not re.fullmatch(r"[0-9a-f]{32}", source_id):
                    continue
                meta = conn.execute("SELECT protocol,model FROM api_source_meta_v1 "
                                    "WHERE account=? AND source_id=?", (account, source_id)).fetchone()
                insights = conn.execute("SELECT COUNT(*) FROM (SELECT session,id FROM api_insights_v1 "
                                        "WHERE account=? AND source_id LIKE ? GROUP BY session,id)",
                                        (account, source_id + ":%")).fetchone()[0]
                portraits = conn.execute("SELECT COUNT(*) FROM (SELECT session,subject FROM api_portrait_v1 "
                                         "WHERE account=? AND source_id LIKE ? GROUP BY session,subject)",
                                         (account, source_id + ":%")).fetchone()[0]
                suspended = conn.execute("SELECT 1 FROM analysis_cache_suspended_v1 "
                                         "WHERE account=? AND source_id=?",
                                         (account, source_id)).fetchone() is not None
                sources.append({"sourceId": source_id, "kind": "api",
                                "label": meta[1] if meta else "旧 API 来源",
                                **({"protocol": meta[0]} if meta else {}),
                                "messageCount": insights, "portraitCount": portraits,
                                "suspended": suspended})
        return sources

    def clear_analysis_cache(self, account, source_id):
        with self.connect() as conn:
            if source_id == LOCAL_SOURCE_ID:
                present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("results_v2", "analysis_skips", "fine_results_v1", "fine_skips_v1",
                              "progress_v1", "summary_v1", "profile_tokens_v1",
                              "profile_tokens_ready_v1", "profile_state_v1", "batch_progress_v1",
                              "batch_runs_v1", "batch_coverage_v1", "batch_fragments_v1",
                              "quoted_backfill_v1", "qq_analysis_generations_v1"):
                    if table in present:
                        conn.execute(f"DELETE FROM {table} WHERE account=?", (account,))
            else:
                for table in ("api_insights_v1", "api_portrait_v1", "qq_api_insight_basis_v1"):
                    conn.execute(f"DELETE FROM {table} WHERE account=? AND source_id LIKE ?",
                                 (account, source_id + ":%"))
                # A saved source record with zero rows may still carry stale config/job
                # state, so clearing it must remove that record too.
                conn.execute("DELETE FROM api_source_meta_v1 WHERE account=? AND source_id=?",
                             (account, source_id))
                conn.execute("DELETE FROM qq_api_generations_v1 WHERE account=? AND source_id=?",
                             (account, source_id))

    def skip_fine(self, account, user, version, message, reason):
        seq, shard, local_id = message["_sort"]
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO fine_skips_v1 VALUES (?,?,?,?,?,?,?,?)",
                         (account, user, message["id"], version, seq, shard, local_id, reason))

    def text_refs(self, account, user, version, member=None):
        with self.connect() as conn:
            sql = ("SELECT shard,local_id,id FROM results_v2 WHERE account=? AND session=? "
                   "AND version=? AND side='other'")
            args = [account, user, version]
            if member:
                sql += " AND sender=?"
                args.append(member)
            return list(conn.execute(sql, args))

    @staticmethod
    def _add_emotion(summary, emotion, rank, tail=None):
        if not emotion:
            return
        tail = tail or {}
        for entry in emotion:
            raw = entry.get("rawLabel") or entry["label"]
            values = summary["mood"].setdefault(raw, {"label": entry["label"], "sum": 0.0,
                                                       "weighted": 0.0})
            values["sum"] += entry["probability"]
            values["weighted"] += rank * entry["probability"] + tail.get(raw, 0.0)
            values["label"] = entry["label"]
        for raw, probability in tail.items():
            if raw not in {entry.get("rawLabel") or entry["label"] for entry in emotion}:
                summary["mood"][raw]["weighted"] += probability
        summary["moodCount"] += 1

    def _ensure_summary(self, conn, account, user, version):
        row = conn.execute("SELECT score_count,score_sum,score_weighted,mood_count,mood_json "
                           "FROM summary_v1 WHERE account=? AND session=? AND version=?",
                           (account, user, version)).fetchone()
        if row is not None:
            return {"scoreCount": row[0], "scoreSum": row[1], "scoreWeighted": row[2],
                    "moodCount": row[3], "mood": json.loads(row[4])}
        summary = {"scoreCount": 0, "scoreSum": 0.0, "scoreWeighted": 0.0,
                   "moodCount": 0, "mood": {}}
        # Existing installations have results but no summary. This ordered pass happens once
        # for this account/session/version; subsequent reads use only summary_v1.
        for result_json, score, side in conn.execute(
                "SELECT result,score,side FROM results_v2 WHERE account=? AND session=? AND version=? "
                "ORDER BY sort_seq,shard,local_id,id", (account, user, version)):
            if side != "other":
                continue
            if score is not None:
                summary["scoreWeighted"] += summary["scoreCount"] * score
                summary["scoreSum"] += score
                summary["scoreCount"] += 1
            emotion = json.loads(result_json).get("emotion") or []
            self._add_emotion(summary, emotion, summary["moodCount"])
        conn.execute("INSERT INTO summary_v1 VALUES (?,?,?,?,?,?,?,?)",
                     (account, user, version, summary["scoreCount"], summary["scoreSum"],
                      summary["scoreWeighted"], summary["moodCount"],
                      json.dumps(summary["mood"], ensure_ascii=False)))
        return summary

    def summary(self, account, user, version):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._ensure_summary(conn, account, user, version)

    def save(self, account, user, version, message, result):
        seq, shard, local_id = message["_sort"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            summary = self._ensure_summary(conn, account, user, version)
            if conn.execute("SELECT 1 FROM results_v2 WHERE account=? AND session=? AND id=? AND version=?",
                            (account, user, message["id"], version)).fetchone() is not None:
                return
            score = result.get("score") if message["side"] == "other" else None
            mood = (result.get("emotion") or []) if message["side"] == "other" else []
            rank = tail_score = 0
            tail_mood = {}
            if score is not None:
                position = (seq, shard, local_id, message["id"])
                prefix = (account, user, version)
                newest = conn.execute(
                    "SELECT sort_seq,shard,local_id,id FROM results_v2 WHERE account=? AND session=? "
                    "AND version=? AND side='other' AND score IS NOT NULL "
                    "ORDER BY sort_seq DESC,shard DESC,local_id DESC,id DESC LIMIT 1", prefix,
                ).fetchone()
                if newest is None or position > tuple(newest):
                    rank = summary["scoreCount"]
                else:
                    rank = conn.execute(
                        "SELECT COUNT(*) FROM results_v2 WHERE account=? AND session=? AND version=? "
                        "AND side='other' AND score IS NOT NULL AND (sort_seq,shard,local_id,id)<(?,?,?,?)",
                        (*prefix, *position),
                    ).fetchone()[0]
                    tails = conn.execute(
                        "SELECT result,score FROM results_v2 WHERE account=? AND session=? AND version=? "
                        "AND side='other' AND score IS NOT NULL AND (sort_seq,shard,local_id,id)>(?,?,?,?)",
                        (*prefix, *position),
                    )
                    for tail_json, tail_score_value in tails:
                        tail_score += tail_score_value
                        for entry in json.loads(tail_json).get("emotion") or []:
                            raw = entry.get("rawLabel") or entry["label"]
                            tail_mood[raw] = tail_mood.get(raw, 0.0) + entry["probability"]
            inserted = conn.execute("INSERT OR IGNORE INTO results_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                    (account, user, message["id"], seq, shard, local_id, message["senderId"],
                                     message["side"], json.dumps(result, ensure_ascii=False), score, version))
            if inserted.rowcount != 1:
                return
            words = keyword_counts([message.get("text", "")]) if message["side"] == "other" else {}
            conn.execute("INSERT OR IGNORE INTO profile_tokens_v1 VALUES (?,?,?,?,?)",
                         (account, user, version, message["id"], json.dumps(words, ensure_ascii=False)))
            if score is not None:
                summary["scoreWeighted"] += rank * score + tail_score
                summary["scoreSum"] += score
                summary["scoreCount"] += 1
            if mood:
                self._add_emotion(summary, mood, rank, tail_mood)
            conn.execute("UPDATE summary_v1 SET score_count=?,score_sum=?,score_weighted=?,mood_count=?,mood_json=? "
                         "WHERE account=? AND session=? AND version=?",
                         (summary["scoreCount"], summary["scoreSum"], summary["scoreWeighted"],
                          summary["moodCount"], json.dumps(summary["mood"], ensure_ascii=False),
                          account, user, version))

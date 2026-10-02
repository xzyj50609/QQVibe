"""Per-WeChat-account selection for conversations shown in the chat sidebar.

Only session identifiers are stored here. Removing a selection never touches
WeChat source files, imported messages, or analysis results.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from account_store import _check_root, _regular, _safe_account, account_id
from qq_identity import is_account_key


META_TABLE = "conversation_selection_meta_v1"
SELECTION_TABLE = "conversation_selection_v1"
READ_TABLE='qq_conversation_read_v1'


class SelectionCorrupt(RuntimeError):
    """The account's selection tables cannot be trusted as a saved choice."""


def _session_id(session):
    if (not isinstance(session, str) or not 1 <= len(session) <= 256 or
            any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in session)):
        raise ValueError("invalid session")
    return session


class ConversationSelectionStore:
    def __init__(self, data_dir):
        self.data_dir = Path(os.path.abspath(data_dir))
        self.lock = threading.RLock()

    def _path(self, account):
        if not (_safe_account(account) or is_account_key(account)):
            raise ValueError("invalid account")
        _check_root(self.data_dir)
        path = self.data_dir / (account_id(account) + ".sqlite3")
        _regular(path)  # Reject an existing symlink, junction, or non-file.
        return path

    @staticmethod
    def _state(conn, account):
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (META_TABLE, SELECTION_TABLE))}
        if not tables:
            return {"account": account, "initialized": False, "selectedSessions": []}
        if tables != {META_TABLE, SELECTION_TABLE}:
            raise SelectionCorrupt("conversation selection schema incomplete")
        markers = conn.execute(
            "SELECT account,version FROM conversation_selection_meta_v1 LIMIT 2").fetchall()
        if not markers:
            if conn.execute("SELECT 1 FROM conversation_selection_v1 LIMIT 1").fetchone():
                raise SelectionCorrupt("conversation selection has no account marker")
            return {"account": account, "initialized": False, "selectedSessions": []}
        if markers != [(account, 1)]:
            raise SelectionCorrupt("conversation selection account mismatch")
        selected = [row[0] for row in conn.execute(
            "SELECT session FROM conversation_selection_v1 WHERE account=? ORDER BY session",
            (account,))]
        return {"account": account, "initialized": True, "selectedSessions": selected}

    def get(self, account):
        with self.lock:
            path = self._path(account)
            if not path.is_file():
                return {"account": account, "initialized": False, "selectedSessions": []}
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)) as conn:
                    return self._state(conn, account)
            except sqlite3.DatabaseError as exc:
                raise SelectionCorrupt("conversation selection unreadable") from exc

    def set_selected(self, account, session, selected):
        _session_id(session)
        if type(selected) is not bool:
            raise ValueError("invalid selected")
        with self.lock:
            path = self._path(account)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _check_root(self.data_dir)
            try:
                with closing(sqlite3.connect(path, timeout=15)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._state(conn, account)  # Never silently repair a partial or mismatched schema.
                    conn.execute("CREATE TABLE IF NOT EXISTS conversation_selection_meta_v1 ("
                                 "account TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version=1))")
                    conn.execute("CREATE TABLE IF NOT EXISTS conversation_selection_v1 ("
                                 "account TEXT NOT NULL, session TEXT NOT NULL, "
                                 "PRIMARY KEY(account,session), "
                                 "FOREIGN KEY(account) REFERENCES conversation_selection_meta_v1(account))")
                    conn.execute("INSERT OR IGNORE INTO conversation_selection_meta_v1 VALUES (?,1)",
                                 (account,))
                    if selected:
                        conn.execute("INSERT OR IGNORE INTO conversation_selection_v1 VALUES (?,?)",
                                     (account, session))
                    else:
                        conn.execute("DELETE FROM conversation_selection_v1 WHERE account=? AND session=?",
                                     (account, session))
                    state = self._state(conn, account)
                    conn.commit()
                    return state
            except sqlite3.DatabaseError as exc:
                raise SelectionCorrupt("conversation selection cannot be saved") from exc

    def paused_reading(self,account):
        with self.lock:
            path=self._path(account)
            if not path.is_file():return set()
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(READ_TABLE,)).fetchone():return set()
                columns=[row[1] for row in conn.execute('PRAGMA table_info('+READ_TABLE+')')]
                if columns!=['account','session','reading']:raise SelectionCorrupt('conversation read control is invalid')
                rows=conn.execute('SELECT session,reading FROM '+READ_TABLE+' WHERE account=?',(account,)).fetchall()
                if any(reading not in (0,1) for _,reading in rows):raise SelectionCorrupt('conversation read control is invalid')
                return {session for session,reading in rows if not reading}

    def set_reading(self,account,session,enabled):
        _session_id(session)
        if type(enabled) is not bool:raise ValueError('invalid-reading-choice')
        with self.lock:
            if session not in self.get(account)['selectedSessions']:raise ValueError('conversation-not-selected')
            self.paused_reading(account)
            with closing(sqlite3.connect(self._path(account),timeout=15)) as conn,conn:
                conn.execute('CREATE TABLE IF NOT EXISTS '+READ_TABLE+' (account TEXT NOT NULL,session TEXT NOT NULL,'
                    'reading INTEGER NOT NULL CHECK(reading IN (0,1)),PRIMARY KEY(account,session))')
                conn.execute('INSERT INTO '+READ_TABLE+' VALUES (?,?,?) ON CONFLICT(account,session) DO UPDATE SET reading=excluded.reading',
                    (account,session,int(enabled)))
        return {'account':account,'user':session,'readEnabled':enabled}

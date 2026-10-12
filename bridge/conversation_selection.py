"""Per-WeChat-account selection for conversations shown in the chat sidebar.

Only session identifiers are stored here. Removing a selection never touches
WeChat source files, imported messages, or analysis results.

The same rows carry the per-conversation analysis request: `requested` marks the
conversations the user asked to be analysed with the card button. Adding a
conversation to the sidebar is not a request to analyse it, so `requested`
defaults to 0 and `set_all_selected` never sets it. Because the flag lives on the
selection row, removing a conversation from the sidebar also drops its request,
which keeps `requested` a subset of `selected` by construction.
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


def _columns(conn, table):
    """Column names of an existing table. An absent table reports no columns."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _empty(account):
    return {"account": account, "initialized": False, "selectedSessions": [],
            "requestedSessions": []}


def _ensure_tables(conn):
    """Create the selection tables and add the `requested` column to older databases."""
    conn.execute("CREATE TABLE IF NOT EXISTS conversation_selection_meta_v1 ("
                 "account TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version=1))")
    conn.execute("CREATE TABLE IF NOT EXISTS conversation_selection_v1 ("
                 "account TEXT NOT NULL, session TEXT NOT NULL, "
                 "PRIMARY KEY(account,session), "
                 "FOREIGN KEY(account) REFERENCES conversation_selection_meta_v1(account))")
    if "requested" not in _columns(conn, SELECTION_TABLE):
        # Written after the CREATE above so a concurrent first open cannot lose the column.
        conn.execute("ALTER TABLE conversation_selection_v1 "
                     "ADD COLUMN requested INTEGER NOT NULL DEFAULT 0")


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
            return _empty(account)
        if tables != {META_TABLE, SELECTION_TABLE}:
            raise SelectionCorrupt("conversation selection schema incomplete")
        markers = conn.execute(
            "SELECT account,version FROM conversation_selection_meta_v1 LIMIT 2").fetchall()
        if not markers:
            if conn.execute("SELECT 1 FROM conversation_selection_v1 LIMIT 1").fetchone():
                raise SelectionCorrupt("conversation selection has no account marker")
            return _empty(account)
        if markers != [(account, 1)]:
            raise SelectionCorrupt("conversation selection account mismatch")
        selected = [row[0] for row in conn.execute(
            "SELECT session FROM conversation_selection_v1 WHERE account=? ORDER BY session",
            (account,))]
        # A database written before the analysis request existed has no `requested` column
        # and therefore no requests. Reading it through a read-only connection must not
        # migrate, so treat the missing column as "nothing requested yet".
        requested = []
        if "requested" in _columns(conn, SELECTION_TABLE):
            requested = [row[0] for row in conn.execute(
                "SELECT session FROM conversation_selection_v1 "
                "WHERE account=? AND requested=1 ORDER BY session", (account,))]
        return {"account": account, "initialized": True, "selectedSessions": selected,
                "requestedSessions": requested}

    def get(self, account):
        with self.lock:
            path = self._path(account)
            if not path.is_file():
                return _empty(account)
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)) as conn:
                    return self._state(conn, account)
            except sqlite3.DatabaseError as exc:
                raise SelectionCorrupt("conversation selection unreadable") from exc

    def _write(self, account, body):
        """Run `body` inside one IMMEDIATE transaction against the account database."""
        with self.lock:
            path = self._path(account)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _check_root(self.data_dir)
            try:
                with closing(sqlite3.connect(path, timeout=15)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._state(conn, account)  # Never silently repair a partial or mismatched schema.
                    _ensure_tables(conn)
                    conn.execute("INSERT OR IGNORE INTO conversation_selection_meta_v1 VALUES (?,1)",
                                 (account,))
                    body(conn)
                    state = self._state(conn, account)
                    conn.commit()
                    return state
            except sqlite3.DatabaseError as exc:
                raise SelectionCorrupt("conversation selection cannot be saved") from exc

    def set_selected(self, account, session, selected):
        _session_id(session)
        if type(selected) is not bool:
            raise ValueError("invalid selected")

        def body(conn):
            if selected:
                # Explicit column list: the row also carries the `requested` flag, so a
                # conversation that is re-added to the sidebar starts out unrequested.
                conn.execute("INSERT OR IGNORE INTO conversation_selection_v1 (account,session) "
                             "VALUES (?,?)", (account, session))
            else:
                conn.execute("DELETE FROM conversation_selection_v1 WHERE account=? AND session=?",
                             (account, session))

        return self._write(account, body)

    def set_requested(self, account, session, requested):
        """Ask for one conversation to be analysed, independent of the background switch.

        Requesting implies the conversation is in the sidebar, since the button only exists
        on a listed card. Removing it from the sidebar deletes the row and with it the
        request, which keeps `requested` a subset of `selected` by construction.
        """
        _session_id(session)
        if type(requested) is not bool:
            raise ValueError("invalid requested")

        def body(conn):
            conn.execute("INSERT OR IGNORE INTO conversation_selection_v1 (account,session) "
                         "VALUES (?,?)", (account, session))
            conn.execute("UPDATE conversation_selection_v1 SET requested=? "
                         "WHERE account=? AND session=?",
                         (1 if requested else 0, account, session))

        return self._write(account, body)

    def set_all_selected(self, account, sessions):
        """Add every known session of the account to the sidebar at once.

        This never requests analysis: being added to the list is not a request to analyse it.
        """
        sessions = [_session_id(session) for session in sessions]

        def body(conn):
            conn.executemany("INSERT OR IGNORE INTO conversation_selection_v1 (account,session) "
                             "VALUES (?,?)", [(account, session) for session in sessions])

        return self._write(account, body)

    def remove_selected(self, account, sessions):
        if (not isinstance(sessions, list) or not 1 <= len(sessions) <= 1000 or
                len(set(_session_id(session) for session in sessions)) != len(sessions)):
            raise ValueError("invalid session selection")
        with self.lock:
            path = self._path(account)
            if not path.is_file():
                raise ValueError("session not selected")
            try:
                with closing(sqlite3.connect(path, timeout=15)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    state = self._state(conn, account)
                    if not set(sessions) <= set(state["selectedSessions"]):
                        raise ValueError("session not selected")
                    conn.executemany("DELETE FROM conversation_selection_v1 WHERE account=? AND session=?",
                                     [(account, session) for session in sessions])
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

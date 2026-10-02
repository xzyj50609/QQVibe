"""Contract tests for account snapshot freshness using only synthetic SQLite data."""

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import snapshot_cache as cache


class Reader(cache.SnapshotCacheMixin):
    def __init__(self, account_dir, workdir, source):
        self.account_dir = str(account_dir)
        self.workdir = str(workdir)
        self.source = source
        self._keys = {}

    def _db_path(self, _rel):
        return str(self.source)

    def _wal_path(self, _rel):
        return None


def make_avatar_db(path, avatar):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE contact (avatar TEXT)")
        connection.execute("INSERT INTO contact VALUES (?)", (avatar,))


def avatar_from(connection):
    try:
        return connection.execute("SELECT avatar FROM contact").fetchone()[0]
    finally:
        connection.close()


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        account_dir = self.root / "account"
        workdir = self.root / "snapshots"
        account_dir.mkdir()
        workdir.mkdir()
        self.source = account_dir / "source.db"
        self.source.write_bytes(b"old source")
        self.reader = Reader(account_dir, workdir, self.source)
        self.new_db = self.root / "fresh.db"
        make_avatar_db(self.new_db, "new-avatar")

    def prepare_stale_snapshot(self, rel):
        destination = cache._destination(self.reader, rel)
        make_avatar_db(destination, "old-avatar")
        self.write_stamp(destination, rel)
        # Size changes make the source signature unambiguously different on Windows.
        self.source.write_bytes(b"changed source with a new avatar")
        self.assertNotEqual(cache._source_signature(self.reader, rel),
                            cache._saved_signature(destination))
        return destination

    def write_stamp(self, destination, rel, applied=0):
        mtime, size, wal_mtime, wal_size = cache._source_signature(self.reader, rel)
        Path(str(destination) + ".stamp").write_text(
            f"{cache.upstream.STAMP_VERSION},{mtime!r},{size},{wal_mtime!r},{wal_size},{applied}",
            encoding="ascii",
        )

    def synthetic_refresh(self, entered=None, release=None, failure=None, applied=0):
        def open_staged(reader, rel):
            if entered:
                entered.set()
            if release and not release.wait(3):
                raise AssertionError("test did not release the snapshot refresh")
            if failure:
                raise failure
            destination = cache._destination(reader, rel)
            shutil.copyfile(self.new_db, destination)
            self.write_stamp(destination, rel, applied)
            return cache._connect(destination)

        return open_staged

    def assert_contact_stale(self, rel):
        try:
            connection = self.reader._open(rel)
        except cache.ContactSnapshotStaleError:
            return
        else:
            self.addCleanup(connection.close)
            self.fail("contact read returned a snapshot that is not current and complete")

    def test_changed_contact_waits_for_fresh_avatar(self):
        rel = os.path.join("contact", "contact.db")
        self.prepare_stale_snapshot(rel)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        outcome = {}

        def read():
            try:
                outcome["avatar"] = avatar_from(self.reader._open(rel))
            except Exception as exc:
                outcome["error"] = exc
            finally:
                done.set()

        with patch.object(cache.upstream.WeChatDB, "_open",
                          self.synthetic_refresh(entered, release)):
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2), "contact refresh did not start")
                self.assertFalse(done.wait(.1), "stale contact was returned before refresh")
            finally:
                release.set()
                thread.join(timeout=3)
                state = cache._state(self.reader, rel)
                if state.future:
                    state.future.result(timeout=3)
            self.assertTrue(done.is_set(), "contact read did not complete after refresh")

        self.assertNotIn("error", outcome)
        self.assertEqual(outcome.get("avatar"), "new-avatar")

    def test_initial_contact_rechecks_source_after_snapshot_build(self):
        rel = os.path.join("contact", "contact.db")
        destination = cache._destination(self.reader, rel)
        self.assertFalse(destination.exists())
        created = []

        def build_then_change_source(reader, selected_rel):
            self.assertEqual(selected_rel, rel)
            signature = cache._source_signature(reader, rel)
            shutil.copyfile(self.new_db, destination)
            self.write_stamp(destination, rel)
            self.assertEqual(cache._saved_signature(destination), signature)
            connection = cache._connect(destination)
            created.append(connection)
            self.source.write_bytes(b"source changed while first contact snapshot was building")
            self.assertNotEqual(cache._source_signature(reader, rel), signature)
            return connection

        with patch.object(cache.upstream.WeChatDB, "_open", build_then_change_source):
            self.assert_contact_stale(rel)
        self.assertEqual(len(created), 1)
        try:
            with self.assertRaises(sqlite3.ProgrammingError):
                created[0].execute("SELECT 1")
        finally:
            created[0].close()

    def test_failed_contact_refresh_does_not_return_old_avatar(self):
        rel = os.path.join("contact", "contact.db")
        self.prepare_stale_snapshot(rel)
        entered = threading.Event()
        with patch.object(cache.upstream.WeChatDB, "_open",
                          self.synthetic_refresh(entered=entered,
                                                 failure=RuntimeError("synthetic refresh failure"))):
            try:
                self.assert_contact_stale(rel)
            finally:
                state = cache._state(self.reader, rel)
                if state.future:
                    state.future.result(timeout=3)
        self.assertTrue(entered.is_set(), "contact refresh was never attempted")

    def test_contact_refresh_timeout_does_not_return_old_avatar(self):
        rel = os.path.join("contact", "contact.db")
        self.prepare_stale_snapshot(rel)

        class TimedOutFuture(Future):
            def __init__(self):
                super().__init__()
                self.waited = False

            def result(self, timeout=None):
                self.waited = True
                raise FutureTimeoutError()

        class TimedOutExecutor:
            def __init__(self):
                self.future = TimedOutFuture()
                self.submitted = False

            def submit(self, *_args):
                self.submitted = True
                return self.future

        executor = TimedOutExecutor()
        with patch.object(cache, "_workers", executor):
            self.assert_contact_stale(rel)
        self.assertTrue(executor.submitted)
        self.assertTrue(executor.future.waited)

    def test_contact_rejects_refresh_with_unapplied_wal(self):
        rel = os.path.join("contact", "contact.db")
        self.prepare_stale_snapshot(rel)
        with patch.object(cache.upstream.WeChatDB, "_open",
                          self.synthetic_refresh(applied=-1)):
            try:
                self.assert_contact_stale(rel)
            finally:
                state = cache._state(self.reader, rel)
                if state.future:
                    state.future.result(timeout=3)

    def test_changed_message_keeps_fast_previous_snapshot(self):
        rel = os.path.join("message", "message_0.db")
        self.prepare_stale_snapshot(rel)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        with patch.object(cache.upstream.WeChatDB, "_open",
                          self.synthetic_refresh(entered, release)):
            self.assertEqual(avatar_from(self.reader._open(rel)), "old-avatar")
            self.assertTrue(entered.wait(2), "message refresh did not start in background")
            release.set()
            state = cache._state(self.reader, rel)
            if state.future:
                state.future.result(timeout=3)


if __name__ == "__main__":
    unittest.main()

"""Synthetic, account-scoped key preparation checks; no real WeChat data."""
import ctypes
import io
import json
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from cache_source import CacheOnlyWeChatDB, IncompleteKeyCache
import live_source
from live_source import (ActiveSelection, LiveWeChatFactory, SessionOnlyWeChatDB,
                         VolatileKeyWeChatDB,
                         _BoundedConfigCipherProbe, _ConfigScanLimit)


class ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class SyntheticMemory:
    """One fake committed region; never opens or queries a real process."""

    def __init__(self, payload, base=0x100000):
        self.payload = bytes(payload)
        self.base = base
        self.queries = 0

    def VirtualQueryEx(self, _handle, address, info_ptr, _size):
        self.queries += 1
        if (address.value or 0) >= self.base + len(self.payload):
            return 0
        info = ctypes.cast(info_ptr, ctypes.POINTER(live_source.upstream_db._MBI)).contents
        info.BaseAddress = self.base
        info.RegionSize = len(self.payload)
        info.State = 0x1000
        info.Protect = 0x04
        return ctypes.sizeof(info)

    def read(self, address, size):
        offset = address - self.base
        if offset < 0 or offset >= len(self.payload):
            return None
        return self.payload[offset:offset + size]


class ConfigCipherProbeTests(unittest.TestCase):
    def make_memory(self, anchor_count=48, pair_count=144):
        name = live_source.upstream_db.CONFIG_CIPHER_NAME
        payload = bytearray(20_000)
        base = 0x100000
        anchors = [base + 128 + index * 128 for index in range(anchor_count)]
        for address in anchors:
            offset = address - base
            payload[offset:offset + len(name)] = name
        expected = {address: [] for address in anchors}
        # The first pair straddles a 127-byte chunk boundary.
        pair_offsets = [127 * 80 - 8] + [11_000 + index * 32 for index in range(pair_count - 1)]
        for index, offset in enumerate(pair_offsets):
            anchor = anchors[index % len(anchors)]
            payload[offset:offset + 16] = struct.pack("<QQ", anchor, len(name))
            expected[anchor].append(base + offset)
        return SyntheticMemory(payload, base), anchors, expected

    def test_more_than_32_anchors_and_128_pairs_use_one_pair_pass(self):
        memory, anchors, expected = self.make_memory()
        probe = _BoundedConfigCipherProbe({})
        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_CHUNK", 127):
            found = probe._find_bytes(1, memory.read, live_source.upstream_db.CONFIG_CIPHER_NAME)
            self.assertEqual(found, anchors)
            actual = {anchor: list(probe._find_bytes(
                1, memory.read,
                struct.pack("<QQ", anchor, len(live_source.upstream_db.CONFIG_CIPHER_NAME))))
                for anchor in anchors}
        self.assertEqual(actual, expected)
        self.assertEqual(memory.queries, 4)  # One region and one terminator per pass.
        self.assertEqual(probe.metrics()["pairs"], 144)
        self.assertEqual(probe.metrics()["limit_hit"], "none")

    def test_anchor_and_pair_caps_report_truncation(self):
        memory, anchors, _expected = self.make_memory(anchor_count=8, pair_count=16)
        probe = _BoundedConfigCipherProbe({})
        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_ANCHORS", 4), \
                patch("live_source.CONFIG_SCAN_PAIR_HITS", 3):
            found = probe._find_bytes(1, memory.read, live_source.upstream_db.CONFIG_CIPHER_NAME)
            self.assertEqual(found, anchors[:4])
            list(probe._find_bytes(1, memory.read,
                                   struct.pack("<QQ", anchors[0],
                                               len(live_source.upstream_db.CONFIG_CIPHER_NAME))))
        self.assertEqual(probe.metrics()["anchors"], 4)
        self.assertEqual(probe.metrics()["pairs"], 3)
        self.assertEqual(probe.metrics()["limit_hit"], "anchor_limit,pair_limit")

    def test_byte_and_time_limits_are_explicit(self):
        memory, _anchors, _expected = self.make_memory(anchor_count=8, pair_count=16)
        probe = _BoundedConfigCipherProbe({})
        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_CHUNK", 64), \
                patch("live_source.CONFIG_SCAN_PASS_BYTES", 256):
            probe._find_bytes(1, memory.read, live_source.upstream_db.CONFIG_CIPHER_NAME)
        self.assertEqual(probe.metrics()["scan_bytes"], 256)
        self.assertEqual(probe.metrics()["limit_hit"], "byte_limit")
        expired = _BoundedConfigCipherProbe({})
        expired._deadline = 0
        with self.assertRaisesRegex(_ConfigScanLimit, "time_limit"):
            expired._find_bytes(1, memory.read, live_source.upstream_db.CONFIG_CIPHER_NAME)
        self.assertEqual(expired.metrics()["limit_hit"], "time_limit")

    def test_region_and_candidate_limits_are_explicit(self):
        memory, _anchors, _expected = self.make_memory(anchor_count=8, pair_count=16)
        probe = _BoundedConfigCipherProbe({})
        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_REGIONS", 1):
            probe._find_bytes(1, memory.read, live_source.upstream_db.CONFIG_CIPHER_NAME)
        self.assertEqual(probe.metrics()["limit_hit"], "region_limit")

        candidate_probe = _BoundedConfigCipherProbe({})
        with patch("live_source.CONFIG_SCAN_CANDIDATES", 1):
            candidate_probe._probable_key(bytes(range(32)))
            with self.assertRaisesRegex(_ConfigScanLimit, "candidate_limit"):
                candidate_probe._probable_key(bytes(range(32)))
        self.assertEqual(candidate_probe.metrics()["candidates"], 1)
        self.assertEqual(candidate_probe.metrics()["limit_hit"], "candidate_limit")

    def test_failed_and_short_large_reads_recover_anchors_and_pairs(self):
        memory, anchors, expected = self.make_memory()
        requests = []

        def short_large_read(address, size):
            requests.append(size)
            if size > 4096:
                return None
            return memory.read(address, min(size, 2048))

        probe = _BoundedConfigCipherProbe({})
        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_CHUNK", 8192):
            found = probe._find_bytes(1, short_large_read,
                                      live_source.upstream_db.CONFIG_CIPHER_NAME)
            actual = {anchor: list(probe._find_bytes(
                1, short_large_read,
                struct.pack("<QQ", anchor, len(live_source.upstream_db.CONFIG_CIPHER_NAME))))
                for anchor in anchors}
        self.assertEqual(found, anchors)
        self.assertEqual(actual, expected)
        self.assertTrue(any(size > 4096 for size in requests))
        self.assertTrue(any(2048 < size <= 4096 for size in requests))
        self.assertTrue(any(size <= 2048 for size in requests))
        self.assertEqual(probe.metrics()["read_gaps"], 0)
        self.assertEqual(probe.metrics()["limit_hit"], "none")

    def test_unreadable_page_is_reported_and_never_joined_across(self):
        size = live_source.PAGE_SIZE
        base = 0x100000
        name = live_source.upstream_db.CONFIG_CIPHER_NAME
        payload = bytearray(size * 4)
        anchors = [base + 128, base + size * 2 + 128]
        for address in anchors:
            offset = address - base
            payload[offset:offset + len(name)] = name
            payload[offset + 128:offset + 144] = struct.pack("<QQ", address, len(name))
        split = len(name) // 2
        payload[size - split:size] = name[:split]
        payload[size * 2:size * 2 + len(name) - split] = name[split:]
        memory = SyntheticMemory(payload, base)
        gap_start, gap_end = base + size, base + size * 2

        def unreadable_page(address, count):
            if address < gap_end and address + count > gap_start:
                return None
            return memory.read(address, count)

        probe = _BoundedConfigCipherProbe({})
        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_CHUNK", size * 4):
            found = probe._find_bytes(1, unreadable_page, name)
            actual = {anchor: list(probe._find_bytes(
                1, unreadable_page, struct.pack("<QQ", anchor, len(name))))
                for anchor in anchors}
        self.assertEqual(found, anchors)
        self.assertEqual(actual, {anchor: [anchor + 128] for anchor in anchors})
        self.assertEqual(probe.metrics()["read_gaps"], 2)
        self.assertEqual(probe.metrics()["limit_hit"], "read_gap")

    def test_short_read_retries_consume_the_same_byte_budget(self):
        memory, _anchors, _expected = self.make_memory()
        probe = _BoundedConfigCipherProbe({})

        def short_large_read(address, size):
            return memory.read(address, min(size, 2048))

        with patch("live_source.upstream_db._k32", memory), \
                patch("live_source.CONFIG_SCAN_CHUNK", 8192), \
                patch("live_source.CONFIG_SCAN_PASS_BYTES", 9000):
            probe._find_bytes(1, short_large_read,
                              live_source.upstream_db.CONFIG_CIPHER_NAME)
        self.assertEqual(probe.metrics()["scan_bytes"], 9000)
        self.assertEqual(probe.metrics()["read_gaps"], 0)
        self.assertEqual(probe.metrics()["limit_hit"], "byte_limit")


class LiveKeyPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_dir = self.root / "accounts"
        self.account = "synthetic-private-account"
        self.location = self.db_dir / self.account
        self.storage = self.location / "db_storage"
        self.stable = self.root / "stable"
        self.stable.mkdir()
        self.selection = ActiveSelection(self.location.resolve(), ((12345, 100.0),))
        self.files = {}
        for category, name, marker in (("session", "session.db", 1),
                                       ("contact", "contact.db", 2),
                                       ("message", "message_0.db", 3)):
            path = self.storage / category / name
            path.parent.mkdir(parents=True)
            path.write_bytes(b"S" * 16 + bytes([marker]) + b"P" * (4096 - 17))
            self.files[os.path.join(category, name)] = (path, marker)
        self.keys = {rel: bytes([marker]) * 32 for rel, (_path, marker) in self.files.items()}
        self.stable_file = self.stable / (self.account + ".json")
        self.log = io.StringIO()
        self.verify = patch("live_source.upstream_db._verify_enc_key",
                            side_effect=lambda key, page: key[:1] == page[16:17])
        self.verify.start()
        self.addCleanup(self.verify.stop)
        self.env = patch.dict("os.environ", {"WECHATAUTO_KEYS_DIR": str(self.stable)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.snapshot = patch("live_source.active_account_snapshot", return_value=self.selection)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)
        self.thread = patch("live_source.threading.Thread", ImmediateThread)
        self.thread.start()
        self.addCleanup(self.thread.stop)

    def save_cache(self, keys):
        self.stable_file.write_text(json.dumps({rel: key.hex() for rel, key in keys.items()}),
                                    encoding="utf-8")

    def prepare(self, found):
        factory = LiveWeChatFactory(retry_seconds=30)
        with patch("live_source._scan_config_cipher_keys", return_value=found), redirect_stderr(self.log):
            with self.assertRaisesRegex(RuntimeError, "preparation pending"):
                factory(db_dir=str(self.db_dir), account=self.account, selection=self.selection)
        return factory

    def assert_private_log(self):
        log = self.log.getvalue()
        self.assertEqual(len(log.splitlines()), 1)
        self.assertNotIn(self.account, log)
        self.assertNotIn(str(self.root), log)
        self.assertNotIn(".db", log)
        self.assertNotIn("synthetic-private", log)
        for rel in self.files:
            self.assertNotIn(rel, log)
        for key in self.keys.values():
            self.assertNotIn(key.hex(), log)

    def test_cache_and_config_cipher_scan_are_merged_for_selected_account(self):
        session, contact, message = self.files
        self.save_cache({session: self.keys[session], contact: self.keys[contact]})
        overlapping_scan_key = bytes([1]) + b"X" * 31
        factory = self.prepare({session: overlapping_scan_key, message: self.keys[message]})
        reader = factory(db_dir=str(self.db_dir), account=self.account, selection=self.selection)
        self.assertIsInstance(reader, VolatileKeyWeChatDB)
        self.assertEqual(reader._keys, self.keys)
        self.assertIn("result=ready required=3 matched=3 cache=2 scan_new=1 union=3", self.log.getvalue())
        self.assertIn("session=1/1 contact=1/1 message=1/1 other=0/0", self.log.getvalue())
        self.assert_private_log()

    def test_wrong_message_key_keeps_only_validated_session_reader(self):
        session, contact, message = self.files
        wrong = b"W" * 32
        self.save_cache({session: self.keys[session], contact: wrong})
        with self.assertRaises(IncompleteKeyCache) as caught:
            CacheOnlyWeChatDB(db_dir=str(self.db_dir), account=self.account)
        self.assertEqual(caught.exception.validated_keys, {session: self.keys[session]})

        factory = self.prepare({contact: self.keys[contact], message: wrong})
        slot = next(iter(factory.slots.values()))
        self.assertEqual((slot.state, slot.keys),
                         ("sessions", {session: self.keys[session], contact: self.keys[contact]}))
        reader = factory(db_dir=str(self.db_dir), account=self.account, selection=self.selection)
        self.assertIsInstance(reader, SessionOnlyWeChatDB)
        self.assertFalse(reader.messages_ready)
        with self.assertRaisesRegex(RuntimeError, "messages unavailable"):
            reader._message_dbs()
        self.assertIn("result=keys_incomplete required=3 matched=2 cache=1 scan_new=1 union=2",
                      self.log.getvalue())
        self.assertIn("session=1/1 contact=1/1 message=0/1 other=0/0", self.log.getvalue())
        self.assert_private_log()

    def test_page_change_rejects_scanned_key_before_reader_is_returned(self):
        session, contact, message = self.files
        self.save_cache({session: self.keys[session], contact: self.keys[contact]})
        factory = self.prepare({message: self.keys[message]})
        path = self.files[message][0]
        changed = bytearray(path.read_bytes())
        changed[16] = 9  # Keep the page-salt token unchanged, but invalidate the key.
        path.write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError, "preparation pending"):
            factory(db_dir=str(self.db_dir), account=self.account, selection=self.selection)
        self.assertEqual(next(iter(factory.slots.values())).state, "failed")

    def test_incomplete_bounded_scan_reports_limit_instead_of_missing_keys(self):
        session = next(iter(self.files))
        for limit in ("pair_limit", "read_gap"):
            with self.subTest(limit=limit):
                self.log = io.StringIO()

                def partial_scan(_pid, _pages, metrics):
                    metrics.update({"anchors": 48, "pairs": 3,
                                    "read_gaps": int(limit == "read_gap"),
                                    "limit_hit": limit})
                    return {session: self.keys[session]}

                factory = LiveWeChatFactory(retry_seconds=30)
                with patch("live_source._scan_config_cipher_keys", side_effect=partial_scan), \
                        redirect_stderr(self.log):
                    with self.assertRaisesRegex(RuntimeError, "preparation pending"):
                        factory(db_dir=str(self.db_dir), account=self.account,
                                selection=self.selection)
                self.assertEqual(next(iter(factory.slots.values())).state, "failed")
                self.assertIn("result=scan_limit required=3 matched=1", self.log.getvalue())
                self.assertIn("anchors=48 pairs=3", self.log.getvalue())
                self.assertIn(f"read_gaps={int(limit == 'read_gap')}", self.log.getvalue())
                self.assertIn(f"limit_hit={limit}", self.log.getvalue())
                self.assert_private_log()


if __name__ == "__main__":
    unittest.main()

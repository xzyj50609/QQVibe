"""Batch context uses the same injectable column cipher as its result store."""
import json
import tempfile
import unittest
from pathlib import Path

from batch_state import BatchStateStore
from payload_crypto import ENVELOPE, PayloadUnreadable
from profile_state import empty_state
from result_store import ResultStore
from test_payload_crypto import cipher


class BatchPayloadTests(unittest.TestCase):
    def test_seed_move_commit_reopen_and_legacy_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key.bin"
            store = ResultStore(root / "synthetic.sqlite3", cipher=cipher(key))
            batches = BatchStateStore(store)
            scope = ("account", "friend", "v1", "friend")
            context = [{"text": "synthetic private batch context"}]
            batches.seed(*scope, empty_state(), context=context)
            def raw():
                with store.connect() as conn:
                    return conn.execute("SELECT context_json FROM batch_progress_v1").fetchone()[0]
            self.assertTrue(raw().startswith(ENVELOPE))
            shard = "message__message_0.db"
            batches.move_cursor(*scope, (1, shard, 1), context)
            self.assertTrue(raw().startswith(ENVELOPE))
            record = {"id": "m2", "position": [2, shard, 2], "senderId": "me", "side": "self",
                      "target": False, "startOffset": 0, "endOffset": 1, "textLength": 1, "complete": True}
            batches.commit(*scope, batch_id="b2", consumed=[record], cursor=(2, shard, 2),
                           char_offset=0, context=context, result=None)
            self.assertTrue(raw().startswith(ENVELOPE))
            reopened = BatchStateStore(ResultStore(store.path, cipher=cipher(key)))
            self.assertEqual(reopened.load(*scope)["context"], context)
            with store.connect() as conn:
                conn.execute("UPDATE batch_progress_v1 SET context_json=?", (json.dumps(context),))
            self.assertEqual(reopened.load(*scope)["context"], context)
            reopened.move_cursor(*scope, (3, shard, 3), context)
            self.assertTrue(raw().startswith(ENVELOPE))
            key.write_bytes(b"corrupt synthetic key")
            with self.assertRaises(PayloadUnreadable):
                unreadable = BatchStateStore(ResultStore(store.path, cipher=cipher(key)))
                unreadable.load(*scope)


if __name__ == "__main__":
    unittest.main()

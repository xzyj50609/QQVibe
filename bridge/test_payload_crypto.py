"""Column encryption tests; all keys, data and key protectors are synthetic."""
import base64
import json
import multiprocessing
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from payload_crypto import ENVELOPE, PayloadCipher, PayloadUnreadable, default_cipher
from result_store import ResultStore
from guidance_contracts import GUIDANCE_REVISION
from installation_gate import InstallationBusy, InstallationGate
from payload_recovery import recover
from backend_contracts import empty_api_portrait


def others(**overrides):
    advice = {"reading": "对方在确认自己的判断", "strategies": ["先给结论"],
              "replies": [{"tone": "稳妥", "text": "收到，我改好后今天发您"}]}
    advice.update(overrides)
    return advice


def guidance(**overrides):
    value = {"version": GUIDANCE_REVISION, "scenario": "leader", "analyzeSelf": False,
             "subtexts": [{"id": "m2", "status": "ok", "surface": "再看看",
                           "implied": "希望我先自查再交付", "tactic": "留台阶",
                           "sentiment": {"polarity": "negative", "label": "有点挑剔"}}],
             "advice": {"forOthers": others(), "forSelf": None}}
    value.update(overrides)
    return value


def synthetic_protect(value):
    return b"synthetic-protector:" + value


def synthetic_unprotect(value):
    if not value.startswith(b"synthetic-protector:"):
        raise ValueError("synthetic wrong account")
    return value[len(b"synthetic-protector:"):]


def cipher(path):
    return PayloadCipher(path, protect=synthetic_protect, unprotect=synthetic_unprotect)


def race_writer(path, gate, results, label):
    gate.wait(5)
    results.put((label, cipher(path).dumps({"synthetic": label})))


class PayloadCryptoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key_path = self.root / "runtime" / "analysis-key.bin"

    def test_authenticated_roundtrip_legacy_read_and_nonce(self):
        value = {"text": "合成聊天内容"}
        codec = cipher(self.key_path)
        first, second = codec.dumps(value), codec.dumps(value)
        self.assertTrue(first.startswith(ENVELOPE))
        self.assertNotEqual(first, second)
        self.assertNotIn(value["text"], first)
        self.assertEqual(cipher(self.key_path).loads(first), value)
        self.assertEqual(codec.loads(json.dumps(value)), value)

    def test_simultaneous_first_processes_keep_one_restart_readable_key(self):
        context = multiprocessing.get_context("spawn")
        gate, results = context.Event(), context.Queue()
        children = [context.Process(target=race_writer, args=(self.key_path, gate, results, str(index)))
                    for index in range(2)]
        for child in children:
            child.start()
        gate.set()
        try:
            saved = [results.get(timeout=10) for _child in children]
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            reopened = cipher(self.key_path)
            self.assertEqual(sorted(reopened.loads(raw)["synthetic"] for _label, raw in saved), ["0", "1"])
            self.assertEqual(list(self.key_path.parent.glob("*.tmp")), [])
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(3)
            results.close()

    def test_corrupt_key_is_preserved_and_never_replaced(self):
        saved = cipher(self.key_path).dumps({"synthetic": "text"})
        self.key_path.write_bytes(b"invalid synthetic blob")
        before = self.key_path.read_bytes()
        codec = cipher(self.key_path)
        with self.assertRaises(PayloadUnreadable):
            codec.loads(saved)
        with self.assertRaises(PayloadUnreadable):
            codec.dumps({"new": "text"})
        self.assertEqual(self.key_path.read_bytes(), before)

    def test_missing_key_after_enabling_cannot_be_regenerated(self):
        saved = cipher(self.key_path).dumps({"synthetic": "text"})
        self.key_path.unlink()
        codec = cipher(self.key_path)
        with self.assertRaises(PayloadUnreadable):
            codec.loads(saved)
        with self.assertRaises(PayloadUnreadable):
            codec.dumps({"new": "text"})
        self.assertFalse(self.key_path.exists())

    def test_loaded_cipher_refuses_writes_after_key_loss_corruption_or_replacement(self):
        for mutation in ("delete", "truncate", "replace"):
            with self.subTest(mutation=mutation):
                path = self.key_path.parent / mutation / self.key_path.name
                codec = cipher(path)
                codec.dumps({"synthetic": "first"})
                if mutation == "delete":
                    path.unlink()
                elif mutation == "truncate":
                    path.write_bytes(b"")
                else:
                    path.write_bytes(base64.b64encode(synthetic_protect(b"z" * 32)))
                with self.assertRaises(PayloadUnreadable):
                    codec.dumps({"synthetic": "must not be saved"})

    def test_marker_publication_failure_is_retryable_without_partial_marker(self):
        codec = cipher(self.key_path)
        marker = self.key_path.with_name("payload-format.json")
        from payload_crypto import os as crypto_os
        real_fsync = crypto_os.fsync
        calls = []
        def fail_marker_fsync(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("synthetic marker disk failure")
            real_fsync(descriptor)
        with patch("payload_crypto.os.fsync", side_effect=fail_marker_fsync):
            with self.assertRaises(PayloadUnreadable):
                codec.dumps({"synthetic": "first"})
        self.assertTrue(self.key_path.exists())
        key_before = self.key_path.read_bytes()
        self.assertFalse(marker.exists())
        self.assertEqual(list(marker.parent.glob("*.tmp")), [])
        saved = codec.dumps({"synthetic": "retry"})
        self.assertEqual(cipher(self.key_path).loads(saved), {"synthetic": "retry"})
        self.assertEqual(self.key_path.read_bytes(), key_before)
        marker.write_text('{"schema":', encoding="utf-8")
        with self.assertRaises(PayloadUnreadable):
            codec.dumps({"synthetic": "must not be saved"})

    def test_other_account_and_tampered_envelope_are_explicit_errors(self):
        saved = cipher(self.key_path).dumps({"synthetic": "text"})
        foreign = PayloadCipher(self.key_path, unprotect=lambda _blob: b"x" * 32)
        with self.assertRaises(PayloadUnreadable):
            foreign.loads(saved)
        blob = bytearray(base64.b64decode(saved[len(ENVELOPE):]))
        blob[-1] ^= 1
        with self.assertRaises(PayloadUnreadable):
            cipher(self.key_path).loads(ENVELOPE + base64.b64encode(blob).decode())

    def test_failed_key_publish_cannot_write_any_ciphertext_or_leave_temp_key(self):
        with patch("payload_crypto.os.link", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(PayloadUnreadable):
                cipher(self.key_path).dumps({"synthetic": "text"})
        self.assertFalse(self.key_path.exists())
        self.assertEqual(list(self.key_path.parent.glob("*.tmp")), [])

    def test_default_cipher_is_scoped_to_installation(self):
        self.assertIs(default_cipher(self.root), default_cipher(self.root))
        self.assertIsNot(default_cipher(self.root), default_cipher(self.root / "other"))
        self.assertFalse((self.root / ".local").exists())

    def test_progress_and_guidance_encrypt_columns_and_reopen(self):
        path = self.root / "synthetic.sqlite3"
        store = ResultStore(path, cipher=cipher(self.key_path))
        store.advance("acct", "friend", "v1", (1, "shard", 1), [{"text": "synthetic private text"}])
        value = guidance()
        store.api_guidance_save("acct", "friend", "source", "friend", value)
        with store.connect() as connection:
            progress = connection.execute("SELECT context_json FROM progress_v1").fetchone()[0]
            advice = connection.execute("SELECT payload_json FROM api_guidance_v1").fetchone()[0]
        self.assertTrue(progress.startswith(ENVELOPE))
        self.assertTrue(advice.startswith(ENVELOPE))
        reopened = ResultStore(path, cipher=cipher(self.key_path))
        self.assertEqual(reopened.progress("acct", "friend", "v1")["context"], [{"text": "synthetic private text"}])
        self.assertEqual(reopened.api_guidance_get("acct", "friend", "source", "friend")["guidance"], value)

    def test_portrait_begin_upgrade_checkpoint_and_reopen_encrypt_both_payloads(self):
        path = self.root / "synthetic.sqlite3"
        store = ResultStore(path, cipher=cipher(self.key_path))
        identity = ("acct", "friend", "source", "friend")
        available = {"messageCount": 1, "targetTextCount": 1, "totalChars": 10}
        store.api_portrait_begin(*identity, (1, "shard", 1), None, "synthetic", available, [1])
        store.api_portrait_upgrade_resume(*identity, "synthetic", 0, available,
                                         {"batchIndex": 0, "portraitContext": [{"text": "synthetic"}]})
        store.api_portrait_checkpoint(*identity, 1, empty_api_portrait(), 1, 10, True,
                                     resume={"batchIndex": 1, "portraitContext": [{"text": "synthetic"}]})
        with store.connect() as connection:
            row = connection.execute("SELECT portrait_json,resume_json FROM api_portrait_v1").fetchone()
            self.assertTrue(all(value.startswith(ENVELOPE) for value in row))
        reopened = ResultStore(path, cipher=cipher(self.key_path))
        result = reopened.api_portrait_get(*identity)
        self.assertTrue(result["complete"])
        self.assertEqual(result["resume"]["portraitContext"], [{"text": "synthetic"}])

    def test_unreadable_guidance_does_not_delete_or_turn_into_empty_result(self):
        store = ResultStore(self.root / "synthetic.sqlite3", cipher=cipher(self.key_path))
        store.api_guidance_save("acct", "friend", "source", "friend", guidance())
        self.key_path.write_bytes(b"invalid synthetic key")
        store.cipher = cipher(self.key_path)
        with self.assertRaises(PayloadUnreadable):
            store.api_guidance_get("acct", "friend", "source", "friend")
        with store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_guidance_v1").fetchone()[0], 1)

    def test_legacy_progress_is_readable_and_next_write_is_encrypted(self):
        path = self.root / "synthetic.sqlite3"
        legacy = ResultStore(path)
        legacy.advance("acct", "friend", "v1", (1, "shard", 1), [{"text": "legacy synthetic"}])
        current = ResultStore(path, cipher=cipher(self.key_path))
        self.assertEqual(current.progress("acct", "friend", "v1")["context"], [{"text": "legacy synthetic"}])
        current.advance("acct", "friend", "v1", (2, "shard", 2), [{"text": "next synthetic"}])
        with current.connect() as connection:
            self.assertTrue(connection.execute("SELECT context_json FROM progress_v1").fetchone()[0].startswith(ENVELOPE))

    def test_first_backup_is_consistent_recoverable_and_never_replaced(self):
        path = self.root / "synthetic.sqlite3"
        legacy = ResultStore(path)
        legacy.advance("acct", "friend", "v1", (1, "shard", 1), [{"text": "restore this synthetic text"}])
        current = ResultStore(path, cipher=cipher(self.key_path))
        manifest = json.loads(path.with_suffix(".pre-wv1.json").read_text())
        backup = path.with_suffix(".pre-wv1.sqlite3")
        before = backup.read_bytes()
        current.advance("acct", "friend", "v1", (2, "shard", 2), [{"text": "later encrypted text"}])
        ResultStore(path, cipher=cipher(self.key_path))
        self.assertEqual(backup.read_bytes(), before)
        with closing(sqlite3.connect(backup)) as connection:
            raw = connection.execute("SELECT context_json FROM progress_v1").fetchone()[0]
            self.assertEqual(json.loads(raw), [{"text": "restore this synthetic text"}])
        self.assertEqual((path.parent / manifest["keyBackup"]).read_bytes(), self.key_path.read_bytes())

    def test_backup_manifest_publish_failure_retries_without_replacing_snapshot(self):
        path = self.root / "synthetic.sqlite3"
        ResultStore(path).advance("acct", "friend", "v1", (1, "shard", 1), [{"text": "legacy"}])
        from result_store import os as store_os
        real_link = store_os.link
        def fail_manifest(source, target):
            if Path(target) == path.with_suffix(".pre-wv1.json"):
                raise OSError("synthetic manifest publication failure")
            return real_link(source, target)
        with patch("result_store.os.link", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                ResultStore(path, cipher=cipher(self.key_path))
        backup = path.with_suffix(".pre-wv1.sqlite3")
        before = backup.read_bytes()
        self.assertFalse(path.with_suffix(".pre-wv1.json").exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        ResultStore(path, cipher=cipher(self.key_path))
        self.assertEqual(backup.read_bytes(), before)
        manifest = path.with_suffix(".pre-wv1.json")
        saved = json.loads(manifest.read_text())
        saved["keySha256"] = "0" * 64
        manifest.write_text(json.dumps(saved))
        with self.assertRaises(PayloadUnreadable):
            ResultStore(path, cipher=cipher(self.key_path))

    def recovery_fixture(self):
        path = self.root / "QQVibeData" / "real-client-data" / ("a" * 64 + ".sqlite3")
        path.parent.mkdir(parents=True)
        self.key_path = self.root / "QQVibeData" / "real-client-runtime" / "analysis-key.bin"
        ResultStore(path).advance("acct", "friend", "v1", (1, "shard", 1), [{"text": "old"}])
        store = ResultStore(path, cipher=cipher(self.key_path))
        store.advance("acct", "friend", "v1", (2, "shard", 2), [{"text": "new"}])
        gate = InstallationGate(self.root, directory=self.root / "synthetic-gate")
        return path, gate

    def test_recovery_defaults_to_validation_then_restores_with_current_data_retained(self):
        path, gate = self.recovery_fixture()
        before = path.read_bytes()
        check = recover(self.root, path.name, unprotect=synthetic_unprotect, gate=gate)
        self.assertFalse(check["applied"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_suffix(".restore-undo.sqlite3").exists())
        self.key_path.write_bytes(b"synthetic corrupt current key")
        restored = recover(self.root, path.name, apply=True, unprotect=synthetic_unprotect, gate=gate)
        self.assertTrue(restored["applied"])
        current = ResultStore(path, cipher=cipher(self.key_path))
        self.assertEqual(current.progress("acct", "friend", "v1")["context"], [{"text": "old"}])
        with closing(sqlite3.connect(path.with_suffix(".restore-undo.sqlite3"))) as connection:
            raw = connection.execute("SELECT context_json FROM progress_v1").fetchone()[0]
            self.assertEqual(cipher(self.key_path).loads(raw), [{"text": "new"}])
        self.assertEqual(path.with_suffix(".restore-undo.key.bin").read_bytes(), b"synthetic corrupt current key")
        self.assertTrue(self.key_path.with_name("payload-format.json").exists())
        with self.assertRaises(PayloadUnreadable):
            recover(self.root, path.name, apply=True, unprotect=synthetic_unprotect, gate=gate)

    def test_recovery_refuses_active_installation_before_writing(self):
        path, gate = self.recovery_fixture()
        before = path.read_bytes()
        with patch.object(gate, "claim", side_effect=InstallationBusy("synthetic peer alive")):
            with self.assertRaises(InstallationBusy):
                recover(self.root, path.name, apply=True, unprotect=synthetic_unprotect, gate=gate)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_suffix(".restore-undo.sqlite3").exists())

    def test_recovery_rejects_manifest_redirection_or_foreign_key_without_writes(self):
        path, gate = self.recovery_fixture()
        before = path.read_bytes()
        with self.assertRaises(PayloadUnreadable):
            recover(self.root, path.name, apply=True, unprotect=lambda _blob: b"wrong", gate=gate)
        manifest = path.with_suffix(".pre-wv1.json")
        saved = json.loads(manifest.read_text())
        saved["keyBackup"] = "../../outside/analysis-key.bin"
        manifest.write_text(json.dumps(saved))
        with self.assertRaises(PayloadUnreadable):
            recover(self.root, path.name, apply=True, unprotect=synthetic_unprotect, gate=gate)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_suffix(".restore-undo.sqlite3").exists())
        with self.assertRaises(ValueError):
            recover(self.root, "../outside.sqlite3", unprotect=synthetic_unprotect, gate=gate)


if __name__ == "__main__":
    unittest.main()

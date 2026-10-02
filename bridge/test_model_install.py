"""Pinned installation/selection failure cases on small synthetic ZIPs only."""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import model_bundle
import model_install
from local_model_source import ModelSource


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qq-model-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = model_install.PRODUCT.data_root(self.root) / "model-downloads"
        self.downloads.mkdir(parents=True)
        self.archive = self.downloads / "synthetic.zip"
        self.files = {"model.onnx": b"synthetic pinned model", "tokenizer/tokenizer.json": b"synthetic tokenizer"}
        self.pins = {name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                     for name, value in self.files.items()}
        self.addCleanup(patch.stopall)
        patch.object(model_install, "pinned_files", return_value=self.pins).start()
        patch.object(model_bundle, "pinned_files", return_value=self.pins).start()
        self.target = model_install.PRODUCT.data_root(self.root) / "models/laya"

    def zipped(self, values=None):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, value in (values or self.files).items(): archive.writestr("laya/" + name, value)

    def test_valid_archive_installs_atomically_and_selects_after_validation(self):
        self.zipped()
        self.assertEqual(model_install.install(self.archive, self.root), self.target)
        self.assertEqual(model_bundle.validate_model_dir(self.target), self.target)
        source = ModelSource(self.root)
        self.assertEqual(source.select("downloaded")["state"], "ready")
        config = source.config.read_bytes()
        invalid = self.root / "invalid"; invalid.mkdir()
        with self.assertRaises(model_bundle.ModelBundleError): source.select(str(invalid))
        self.assertEqual(source.config.read_bytes(), config)

    def test_existing_pinned_model_is_kept_without_opening_a_bad_archive(self):
        self.zipped(); model_install.install(self.archive, self.root)
        self.archive.write_bytes(b"not a zip")
        self.assertEqual(model_install.install(self.archive, self.root), self.target)
        for name, value in self.files.items(): self.assertEqual((self.target / name).read_bytes(), value)

    def test_hash_failure_leaves_no_partial_install_and_retry_succeeds(self):
        bad = dict(self.files); bad["model.onnx"] = b"X" * len(bad["model.onnx"])
        self.zipped(bad)
        with self.assertRaises(model_bundle.ModelBundleError): model_install.install(self.archive, self.root)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])
        self.zipped(); model_install.install(self.archive, self.root)
        self.assertEqual(model_bundle.validate_model_dir(self.target), self.target)

    def test_archive_extra_paths_cannot_escape_or_publish(self):
        self.zipped({**self.files, "../../outside.txt": b"forbidden"})
        with self.assertRaises(model_bundle.ModelBundleError): model_install.install(self.archive, self.root)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_incomplete_old_directory_is_preserved_on_failed_install(self):
        self.target.mkdir(parents=True); old = self.target / "old.dat"; old.write_bytes(b"retain")
        self.zipped()
        with self.assertRaises(model_bundle.ModelBundleError): model_install.install(self.archive, self.root)
        self.assertEqual(old.read_bytes(), b"retain")


if __name__ == "__main__":
    unittest.main()

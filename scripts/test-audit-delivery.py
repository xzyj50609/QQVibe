"""Package scope and redaction checks using synthetic files and distributions."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("delivery_audit_test", "audit-delivery.py")
stage = load("runtime_audit_test", "stage-real-runtime.py")


class DeliveryAuditTests(unittest.TestCase):
    def test_optional_opencv_video_dll_omitted_but_image_runtime_retained(self):
        self.assertFalse(stage.python_record_file_matches(Path("cv2/opencv_videoio_ffmpeg4130_64.dll")))
        self.assertTrue(stage.python_record_file_matches(Path("cv2/cv2.pyd")))
        self.assertTrue(stage.python_record_file_matches(Path("cv2/LICENSE-3RD-PARTY.txt")))
        self.assertTrue(stage.python_record_file_matches(Path("other/library.dll")))

    def test_source_origin_separates_checkout_newlines_from_code_changes(self):
        self.assertEqual(audit.source_origin(b"a\nb\n", b"a\r\nb\r\n", "a.py"), "upstream-newlines-only")
        self.assertEqual(audit.source_origin(b"a\nb\n", b"a\r\nc\r\n", "a.py"), "upstream-modified")
        self.assertEqual(audit.source_origin(b"a", b"a", "a.py"), "upstream-unchanged")

    def test_binary_source_is_never_newline_normalized(self):
        self.assertEqual(audit.source_origin(b"a\nb\n", b"a\r\nb\r\n", "a.png"), "upstream-modified")
        self.assertEqual(audit.source_origin(b"a\0\nb", b"a\0\r\nb", "a.txt"), "upstream-modified")

    def test_optional_gui_packages_omitted_even_when_installed(self):
        installed = {name: SimpleNamespace(files=["module.py"], requires=[]) for name in
                     ["base", "winsdk", "imageio-ffmpeg", "pyautogui"]}
        installed["base"].requires = ["winsdk", "imageio-ffmpeg", "PyAutoGUI", "dev-only; extra == 'test'"]
        with mock.patch.object(stage, "PYTHON_ROOT_PACKAGES", ("base",)), mock.patch.object(
                stage.metadata, "distribution", side_effect=lambda name: installed[name]) as lookup:
            chosen, omitted = stage.python_distributions()
        self.assertEqual(chosen, [installed["base"]])
        self.assertEqual(omitted, ["imageio-ffmpeg", "pyautogui", "winsdk"])
        lookup.assert_called_once_with("base")

    def test_required_missing_distribution_still_rejected(self):
        with mock.patch.object(stage, "PYTHON_ROOT_PACKAGES", ("required",)), mock.patch.object(
                stage.metadata, "distribution", side_effect=stage.metadata.PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "required installed"):
                stage.python_distributions()

    def test_private_paths_rejected_and_public_key_allowed(self):
        for path in ("QQVibeData/a/messages.db", ".local/secret.json", ".env.local", "db-wal.db-wal", "signing.pem"):
            self.assertTrue(audit.private_path(path), path)
        self.assertFalse(audit.private_path("scripts/update-signing.pub"))
        self.assertFalse(audit.private_path("bridge/qq_message_store.py"))

    def test_secret_finding_never_contains_secret_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            secret = "sk-" + "Z" * 40
            path.write_text("key = '" + secret + "'", encoding="utf-8")
            findings = audit.privacy_findings(path, "sample.py")
            self.assertEqual(findings, [{"file": "sample.py", "rule": "literal-api-key"}])
            self.assertNotIn(secret, repr(findings))

    def test_license_spelling_and_binary_are_handled(self):
        self.assertTrue(audit.is_license(Path("LICENCE.md")))
        self.assertTrue(audit.is_license(Path("NOTICE")))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"-----BEGIN PRIVATE KEY-----")
            self.assertEqual(audit.privacy_findings(path, "image.png"), [])

    def test_node_audit_uses_same_scope_as_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            modules = Path(directory); source = modules / "dep"; source.mkdir()
            for name in ("LICENSE", "docs/LICENSE", "node_modules/sub/LICENSE", "tests/LICENSE"):
                path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("license")
            with mock.patch.object(stage, "NODE_MODULES", modules):
                rows = audit.node_licenses(stage, source, {"name": "dep"})
            self.assertEqual([item["file"] for item in rows], ["node_modules/dep/LICENSE"])

    def test_dependency_closure_read_is_nonmutating(self):
        package = {"name": "pkg", "version": "1.0.0", "dependencies": {"nested": "*"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "pkg"; nested = root / "nested"
            for folder, data in ((source, package), (nested, {"name": "nested", "version": "1.0.0"})):
                folder.mkdir(); (folder / "package.json").write_text(__import__('json').dumps(data))
            with mock.patch.object(stage, "NODE_MODULES", root), mock.patch.object(stage, "NODE_ROOT_PACKAGES", ("pkg",)):
                chosen, omitted = stage.node_distributions()
            self.assertEqual(len(chosen), 2); self.assertEqual(omitted, [])
            self.assertEqual(sorted(p.name for p in source.iterdir()), ["package.json"])

    def test_supplemental_license_rejects_version_drift_and_tampering(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder = root / "licenses"; folder.mkdir()
            path = folder / "dep-LICENSE.txt"; path.write_text("license")
            item = audit.record(path, "licenses/dep-LICENSE.txt")
            item.update(package="dep", version="1.0.0", source="https://example.invalid/LICENSE")
            (folder / "supplemental-sources.json").write_text(json.dumps({"files": [item]}))
            self.assertEqual(len(audit.supplemental_licenses("dep", "1.0.0", root)), 1)
            self.assertEqual(audit.supplemental_licenses("dep", "2.0.0", root), [])
            path.write_text("tampered")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                audit.supplemental_licenses("dep", "1.0.0", root)


if __name__ == "__main__":
    unittest.main()

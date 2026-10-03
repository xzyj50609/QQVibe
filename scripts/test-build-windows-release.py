"""Small synthetic tests for the unsigned Windows release archive builder."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).with_name("build-windows-release.py")
SPEC = importlib.util.spec_from_file_location("build_windows_release", SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechatvibe-build-zip-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "win-unpacked"
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("Node.js is unavailable")
        self.add_file(builder.PRODUCT.product_name + ".exe", b"MZsynthetic")
        self.add_file("resources/client/package.json", json.dumps({
            "name": builder.PRODUCT.product_name.lower() + "-runtime", "version": "1.0.2",
        }).encode("utf-8"))
        self.asar_source = self.root / "asar-source"
        for name in builder.ASAR_SCRIPTS:
            payload = (b"-----BEGIN PUBLIC KEY-----\nsynthetic\n-----END PUBLIC KEY-----\n"
                       if name in ("scripts/update-signing.pub", "scripts/qq-update-signing.pub") else
                       f"synthetic {name}".encode("utf-8"))
            self.add_file("resources/client/" + name, payload)
            target = self.asar_source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        self.make_asar()
        self.add_file("resources/client/runtime/python/python.exe", b"MZsynthetic python")
        self.add_file("resources/client/runtime/node/node.exe", b"MZsynthetic node")
        self.add_file("resources/client/scripts/start-real-client.py")
        self.add_file("resources/client/scripts/real-client-update-extract.py")
        self.add_file("resources/client/bridge/chat_server.py")
        self.add_file("resources/client/chatui/index.html")
        self.add_file("resources/client/.models/laya/model.onnx", b"synthetic model")

    def add_file(self, relative: str, payload: bytes = b"synthetic") -> Path:
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def make_asar(self, *, name=None, main="scripts/desktop-main.cjs", version="1.0.2"):
        (self.asar_source / "package.json").write_text(json.dumps({
            "name": name or builder.PRODUCT.product_name.lower(), "main": main, "version": version,
        }), encoding="utf-8")
        archive = self.source / "resources/app.asar"
        archive.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([self.node, "-e",
                        "require('@electron/asar').createPackage(process.argv[1], process.argv[2]).catch(e=>{console.error(e);process.exit(1)})",
                        str(self.asar_source), str(archive)], cwd=builder.ROOT,
                       check=True, capture_output=True, text=True)

    def test_build_is_deterministic_and_extractable(self):
        self.add_file("resources/client/chatui/assets/wechatvibe-icon.png", b"synthetic image")
        first = builder.build_release(self.source, self.root / "first", "1.0.2")
        second = builder.build_release(self.source, self.root / "second", "1.0.2")
        self.assertEqual(first.name, builder.PRODUCT.product_name + "-1.0.2-windows-x64.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertEqual(names[0], "win-unpacked/")
            self.assertTrue(all(name.startswith("win-unpacked/") for name in names))
            self.assertFalse(any(name.startswith("win-unpacked/resources/client/.models/")
                                 for name in names))
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(info.date_time == builder.ZIP_TIME for info in archive.infolist()))
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(builder.extractor.inspect(archive, requirements=builder.REQUIRED_FILES)), len(names))
        candidate = builder.extractor.extract(first, first.parent, "1.0.2", product_name=builder.PRODUCT.product_name)
        self.assertFalse((candidate / "resources/client/.models").exists())

    def test_full_variant_has_distinct_name_and_includes_model(self):
        archive_path = builder.build_release(self.source, self.root / "full", "1.0.2",
                                             with_model=True)
        self.assertEqual(archive_path.name, builder.PRODUCT.product_name + "-1.0.2-windows-x64-full.zip")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.read(
                "win-unpacked/resources/client/.models/laya/model.onnx"), b"synthetic model")

    def test_internal_preview_is_named_and_qq_icons_are_reviewed_assets(self):
        self.add_file("resources/client/chatui/assets/qqvibe-icon.png", b"synthetic image")
        self.add_file("resources/client/chatui/assets/qqvibe-icon.ico", b"synthetic icon")
        archive_path = builder.build_release(self.source, self.root / "internal", "1.0.2",
                                             with_model=True, internal_preview=True)
        self.assertEqual(archive_path.name, builder.PRODUCT.product_name + "-1.0.2-windows-x64-internal-preview-full.zip")

    def test_reviewed_public_demo_image_and_offline_manual_survive_packaging(self):
        image = "resources/client/docs/public/images/overview.png"
        self.add_file(image, b"synthetic reviewed demonstration image")
        self.add_file("使用说明.html", b"<html>offline manual</html>")
        archive_path = builder.build_release(self.source, self.root / "public-guide", "1.0.2")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.read("win-unpacked/" + image), b"synthetic reviewed demonstration image")
            self.assertEqual(archive.read("win-unpacked/使用说明.html"), b"<html>offline manual</html>")

    def test_qq_key_and_brand_screenshots_are_reviewed_but_private_keys_are_not(self):
        self.add_file("resources/client/docs/public/images/chatbean-overview.png", b"reviewed UI demo")
        self.add_file("resources/client/docs/public/images/chatbean-portrait.png", b"reviewed portrait demo")
        archive_path = builder.build_release(self.source, self.root / "chatbean", "1.0.2")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn("win-unpacked/resources/client/scripts/qq-update-signing.pub", archive.namelist())
        self.add_file("resources/client/scripts/qq-update-private.pem", b"private fixture")
        with self.assertRaisesRegex(ValueError, "private or generated"):
            builder.build_release(self.source, self.root / "bad-key", "1.0.2")

    def test_version_and_required_artifacts(self):
        for version in ("1.0.2-preview.1", "v1.0.2", "01.0.2", "1.0.2+build", "1.0.2.3", "1.٠.2"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "stable SemVer"):
                builder.build_release(self.source, self.root / "out", version)
        with self.assertRaisesRegex(ValueError, "version does not match"):
            builder.build_release(self.source, self.root / "out", "1.0.3")
        (self.source / "resources/app.asar").unlink()
        with self.assertRaisesRegex(ValueError, "missing or empty"):
            builder.build_release(self.source, self.root / "out", "1.0.2")
        self.assertFalse((self.root / "out").exists())

    def test_asar_identity_and_updater_scripts_must_match(self):
        for field, value in (("version", "1.0.1"), ("name", "other-app"),
                             ("main", "scripts/other.cjs")):
            with self.subTest(field=field):
                self.make_asar(**{field: value})
                with self.assertRaisesRegex(ValueError, "app.asar identity"):
                    builder.build_release(self.source, self.root / f"wrong-{field}", "1.0.2")
                self.assertFalse((self.root / f"wrong-{field}").exists())
        self.make_asar()
        changed = self.asar_source / "scripts/real-client-update.cjs"
        changed.write_bytes(b"different updater")
        self.make_asar()
        with self.assertRaisesRegex(ValueError, "app.asar script differs"):
            builder.build_release(self.source, self.root / "wrong-script", "1.0.2")
        self.assertFalse((self.root / "wrong-script").exists())

    def test_hardened_extractor_required_file_is_needed(self):
        self.assertEqual(builder.REQUIRED_FILES, builder.extractor.required_files(builder.PRODUCT.product_name))
        missing = self.source / "resources/client/scripts/real-client-update-helper.cjs"
        missing.unlink()
        with self.assertRaisesRegex(ValueError, "missing or empty: resources/client/scripts/real-client-update-helper.cjs"):
            builder.build_release(self.source, self.root / "out", "1.0.2")
        self.assertFalse((self.root / "out").exists())

    def test_private_and_unreviewed_files_are_rejected(self):
        private_paths = (
            "resources/client/docs/public/images/private.png",
            ".local/history.json", "resources/client/account-cache/state.json",
            "resources/client/chats/history.txt", "resources/client/history.db",
            "resources/client/history.sqlite-wal", "resources/client/history.db-journal",
            "resources/client/private.pem",
            "resources/client/auth.json", "resources/client/auth.bin",
            "resources/client/token.dat", "resources/client/api_key.txt",
            "resources/client/id_ed25519.pub",
            "resources/client/.env.production", "resources/client/logs/app.log",
            "resources/client/node_modules/other/lib/cache/state.js",
            "resources/client/app.log.1", "resources/client/screenshot.png",
            "resources/client/IMG_0001.jpg", "resources/client/chatui/assets/IMG_0001.jpg",
        )
        for index, relative in enumerate(private_paths):
            with self.subTest(relative=relative):
                path = self.add_file(relative)
                try:
                    with self.assertRaisesRegex(ValueError, "forbidden"):
                        builder.build_release(self.source, self.root / f"out-{index}", "1.0.2")
                    self.assertFalse((self.root / f"out-{index}").exists())
                finally:
                    path.unlink()

    def test_undici_runtime_cache_modules_are_allowed(self):
        self.add_file("resources/client/node_modules/undici/lib/cache/memory-cache-store.js")
        self.add_file("resources/client/node_modules/undici/lib/web/cache/cachestorage.js")
        archive = builder.build_release(self.source, self.root / "undici", "1.0.2")
        with zipfile.ZipFile(archive) as release:
            self.assertIn("win-unpacked/resources/client/node_modules/undici/lib/cache/memory-cache-store.js",
                          release.namelist())

    def test_sdk_message_code_is_allowed_but_private_data_is_not(self):
        code = "resources/client/node_modules/@anthropic-ai/sdk/resources/messages/index.js"
        self.add_file(code)
        archive = builder.build_release(self.source, self.root / "sdk-code", "1.0.2")
        with zipfile.ZipFile(archive) as release:
            self.assertIn("win-unpacked/" + code, release.namelist())
        private = self.add_file("resources/client/node_modules/@anthropic-ai/sdk/"
                                "resources/messages/private.txt")
        with self.assertRaisesRegex(ValueError, "forbidden"):
            builder.build_release(self.source, self.root / "sdk-private", "1.0.2")
        private.unlink()

    def test_symlink_and_output_containment(self):
        with self.assertRaisesRegex(ValueError, "output directory cannot"):
            builder.build_release(self.source, self.source / "release", "1.0.2")
        target = self.root / "outside.txt"
        target.write_bytes(b"outside")
        link = self.source / "resources/client/link.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this host")
        with self.assertRaisesRegex(ValueError, "symlink or reparse"):
            builder.build_release(self.source, self.root / "out", "1.0.2")

    def test_only_public_signing_key_is_allowed(self):
        public_key = self.add_file("resources/client/scripts/update-signing.pub", b"secret data")
        with self.assertRaisesRegex(ValueError, "public signing key is invalid"):
            builder.build_release(self.source, self.root / "invalid-key", "1.0.2")
        public_key.write_bytes(b"-----BEGIN PUBLIC KEY-----\nsynthetic\n-----END PUBLIC KEY-----\n")
        archive = builder.build_release(self.source, self.root / "valid-key", "1.0.2")
        self.assertTrue(archive.is_file())

    def test_reparse_flag_is_rejected_even_when_symlink_creation_is_unavailable(self):
        fake = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=builder.REPARSE_POINT)
        with self.assertRaisesRegex(ValueError, "symlink or reparse"):
            builder._check_stat(self.source / "junction", fake, directory=False)

    def test_limits_and_atomic_no_overwrite(self):
        with mock.patch.object(builder, "MAX_MEMBERS", 4):
            with self.assertRaisesRegex(ValueError, "member count"):
                builder.build_release(self.source, self.root / "too-many", "1.0.2")
        with mock.patch.object(builder, "MAX_EXPANDED_BYTES", 32):
            with self.assertRaisesRegex(ValueError, "expanded size"):
                builder.build_release(self.source, self.root / "too-large", "1.0.2")
        with mock.patch.object(builder, "MAX_ARCHIVE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "compressed archive"):
                builder.build_release(self.source, self.root / "compressed", "1.0.2")
        self.assertEqual(list((self.root / "compressed").iterdir()), [])
        target = self.root / "existing" / (builder.PRODUCT.product_name + "-1.0.2-windows-x64.zip")
        target.parent.mkdir()
        target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            builder.build_release(self.source, target.parent, "1.0.2")
        self.assertEqual(target.read_bytes(), b"keep")
        with mock.patch.object(builder.os, "link", side_effect=OSError("link unavailable")):
            with self.assertRaisesRegex(OSError, "link unavailable"):
                builder.build_release(self.source, self.root / "atomic", "1.0.2")
        self.assertEqual(list((self.root / "atomic").iterdir()), [])

    def test_extractor_ratio_limit_is_applied(self):
        self.add_file("resources/client/zeroes.dat", b"\0" * (2 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, "compression ratio"):
            builder.build_release(self.source, self.root / "bomb", "1.0.2")
        self.assertEqual(list((self.root / "bomb").iterdir()), [])


if __name__ == "__main__":
    unittest.main()

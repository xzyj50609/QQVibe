"""Synthetic tests for the clean portable staging and package boundary."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = load("stage_real_client", "stage-real-client.py")
builder = load("build_portable_clean", "build-portable-clean.py")


class CleanPortableBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechatvibe-clean-build-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.models = self.root / "external-models"
        self.build = self.root / "build"
        self.client = self.build / "client"
        self.source.mkdir()
        self.models.mkdir()
        self.build.mkdir()
        self.put(self.source / "package.json", b'{"name":"wechatvibe","version":"1.0.2-preview.2"}')
        self.put(self.source / "chatui/index.html", b"<html>reviewed</html>")
        self.put(self.source / "scripts/real-client-update-helper.cjs", b"helper")
        self.put(self.source / "scripts/real-client-update-extract.py", b"extractor")
        self.put(self.source / "scripts/update-signing.pub", b"public key")
        self.put(self.source / "bridge/chat_server.py", b"server")
        self.put(self.source / "bridge/conversation_selection.py", b"selection")
        self.put(self.source / "bridge/unreviewed.py", b"unreviewed")
        self.put(self.source / "chat.db", b"private")
        self.put(self.models / "model.onnx", b"model")
        self.put(self.client / "runtime/python/python.exe", b"python")
        self.put(self.client / "node_modules/dep/index.js", b"node dependency")
        runtime_rows = [self.row(self.client, item) for item in (
            "runtime/python/python.exe", "node_modules/dep/index.js")]
        self.put(self.build / "runtime-manifest.json", json.dumps({"files": runtime_rows}).encode())
        patches = (
            mock.patch.object(stage, "PUBLIC_FILES", ("chatui/index.html",)),
            mock.patch.object(stage, "SCRIPTS", ("real-client-update-helper.cjs",
                                                    "real-client-update-extract.py",
                                                    "update-signing.pub")),
            mock.patch.object(stage, "BRIDGE", ("chat_server.py", "conversation_selection.py")),
            mock.patch.object(stage, "NATIVE_READER", ()),
            mock.patch.object(stage, "LAYA", ()),
            mock.patch.object(stage, "MODEL_FILES", ("model.onnx",)),
            mock.patch.object(stage, "MODEL_PINS", {
                "model.onnx": (5, stage.digest(self.models / "model.onnx")),
            }),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def row(root: Path, relative: str) -> dict:
        path = root / relative
        return {"file": relative, "bytes": path.stat().st_size,
                "sha256": stage.digest(path)}

    def test_new_stage_copies_only_allowlist_and_rejects_reuse(self):
        result = stage.stage_public(self.source, self.models, self.client)
        self.assertEqual(result["sourceVersion"], "1.0.2-preview.2")
        self.assertTrue((self.client / "scripts/real-client-update-helper.cjs").is_file())
        self.assertTrue((self.client / "scripts/real-client-update-extract.py").is_file())
        self.assertTrue((self.client / "scripts/update-signing.pub").is_file())
        self.assertEqual((self.client / "bridge/conversation_selection.py").read_bytes(), b"selection")
        self.assertFalse((self.client / "bridge/unreviewed.py").exists())
        self.assertFalse((self.client / "chat.db").exists())
        with self.assertRaisesRegex(ValueError, "already has a manifest"):
            stage.stage_public(self.source, self.models, self.client)

    def test_unexpected_staged_file_and_directory_are_rejected(self):
        self.put(self.client / "stale.txt", b"old build")
        with self.assertRaisesRegex(ValueError, "unexpected or missing content"):
            stage.stage_public(self.source, self.models, self.client)
        (self.client / "stale.txt").unlink()
        (self.client / "unexpected-empty-dir").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected or missing directories"):
            stage.stage_public(self.source, self.models, self.client)

    def test_standard_stage_does_not_require_or_copy_models(self):
        # A missing directory is valid for API-only users, even with strict model pins.
        result = stage.stage_public(self.source, self.root / "absent-models", self.client,
                                    with_model=False)
        self.assertFalse((self.client / ".models").exists())
        self.assertFalse(any(row["file"].startswith(".models/") for row in result["files"]))
        self.assertTrue((self.client / "bridge/chat_server.py").is_file())

    def test_frozen_export_can_stage_without_git_and_rejects_metadata_tampering(self):
        files = [relative.as_posix() for _, relative, _ in
                 stage.public_mappings(self.source, self.models, with_model=False)] + ["package.json"]
        snapshot = {"sourceCommit": "a" * 40,
                    "files": [self.row(self.source, name) for name in files]}
        self.put(self.source / "PUBLIC_SOURCE_MANIFEST.json", json.dumps(snapshot).encode())
        with self.assertRaisesRegex(ValueError, "provenance differs"):
            stage.stage_public(self.source, self.models, self.client, with_model=False, source_commit="b" * 40)
        self.put(self.source / "package.json", b'{"version":"9.9.9"}')
        with self.assertRaisesRegex(ValueError, "package.json differs"):
            stage.stage_public(self.source, self.models, self.client, with_model=False, source_commit="a" * 40)
        self.put(self.source / "package.json", b'{"name":"wechatvibe","version":"1.0.2-preview.2"}')
        result = stage.stage_public(self.source, self.models, self.client, with_model=False, source_commit="a" * 40)
        self.assertTrue(any(row["file"] == "release-manifest.json" for row in result["files"]))
        self.assertEqual(json.loads((self.client / "release-manifest.json").read_text())["sourceCommit"], "a" * 40)

    def test_changed_runtime_and_model_are_rejected_before_copy(self):
        (self.client / "runtime/python/python.exe").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            stage.stage_public(self.source, self.models, self.client)
        (self.client / "runtime/python/python.exe").write_bytes(b"python")
        (self.models / "model.onnx").write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "pinned model hash differs"):
            stage.stage_public(self.source, self.models, self.client)

    def test_package_hash_and_exact_inventory(self):
        stage.stage_public(self.source, self.models, self.client)
        expected, _ = builder.manifest_files(self.build)
        packaged = self.build / "release/win-unpacked/resources/client"
        shutil.copytree(self.client, packaged)
        builder.verify_tree(packaged, expected)
        self.put(packaged / "logs/private.log", b"private")
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            builder.verify_tree(packaged, expected)
        (packaged / "logs/private.log").unlink()
        (packaged / "logs").rmdir()
        (packaged / "scripts/update-signing.pub").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            builder.verify_tree(packaged, expected)

    def test_packaged_version_and_app_asar_gate(self):
        stage.stage_public(self.source, self.models, self.client)
        unpacked = self.build / "release/win-unpacked"
        shutil.copytree(self.client, unpacked / "resources/client")
        self.put(unpacked / (builder.current_product().product_name + ".exe"), b"MZsynthetic")
        self.put(unpacked / "resources/app.asar", b"synthetic")
        with mock.patch.object(builder, "verify_asar") as asar:
            result = builder.verify_package(self.source, self.build, self.source / "node.exe")
            self.assertEqual(result["version"], "1.0.2-preview.2")
            asar.assert_called_once()
        (unpacked / "resources/client/package.json").write_text('{"version":"0.0.0"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            builder.verify_package(self.source, self.build, self.source / "node.exe")

    def test_generated_config_uses_unique_paths_and_external_electron(self):
        self.put(self.source / "electron-builder.real-client.json", builder.TEMPLATE.read_bytes())
        electron = self.root / "external-electron"
        self.put(electron / "electron.exe", b"MZ")
        generated = builder.generate_config(self.source, self.build, electron)
        config = json.loads(generated.read_text(encoding="utf-8"))
        self.assertEqual(config["directories"]["output"], str(self.build / "release"))
        self.assertEqual(config["electronDist"], str(electron))
        self.assertEqual(config["win"]["icon"], builder.current_product().icon_ico)
        self.assertEqual(config["extraResources"][0]["from"], str(self.client))
        self.assertEqual(config["extraResources"][1]["from"], str(self.client / "node_modules"))
        self.assertEqual(config["extraFiles"], [{"from": str(self.client / "docs/public/USER-GUIDE.html"), "to": "使用说明.html"}])
        template = json.loads((self.source / "electron-builder.real-client.json").read_text())
        self.assertEqual(template["extraResources"][0]["from"], builder.PLACEHOLDERS["stage"])

    def test_build_identity_comes_from_the_same_manifest_for_both_products(self):
        self.put(self.source / "electron-builder.real-client.json", builder.TEMPLATE.read_bytes())
        electron = self.root / "external-electron"
        self.put(electron / "electron.exe", b"MZ")
        for key, name, app_id in (("qq", "QQVibe", "com.local.qqvibe.real-client"),
                                  ("wechat", "WechatVibe", "com.local.wechatvibe.real-client")):
            with self.subTest(product=key), mock.patch.dict(os.environ, {"QQVIBE_PRODUCT": key}):
                config = json.loads(builder.generate_config(self.source, self.build, electron).read_text(encoding="utf-8"))
                self.assertEqual((config["appId"], config["productName"], config["win"]["executableName"]),
                                 (app_id, name, name))
                self.assertEqual(config["extraMetadata"]["name"], name.lower())
                self.assertEqual(config["win"]["icon"], builder.current_product().icon_ico)

    def test_app_asar_relative_dependencies_resolve_in_the_allowlisted_tree(self):
        config = json.loads(builder.TEMPLATE.read_text(encoding="utf-8"))
        app_dir = self.root / "actual-shell-tree"
        edges = []
        for name in config["files"]:
            if "*" in name or name.startswith("!"):
                continue
            self.put(app_dir / name, (builder.ROOT / name).read_bytes())
            if name.endswith(".cjs"):
                source = (builder.ROOT / name).read_text(encoding="utf-8")
                edges += [[str(app_dir / name), dependency] for dependency in
                          re.findall(r'''require\(["'](\./[^"']+)["']\)''', source)]
        self.assertIn([str(app_dir / "scripts/desktop-main.cjs"), "./product-identity.cjs"], edges)
        command = "const {createRequire}=require('node:module');for(const [entry,dep] of JSON.parse(process.argv[1])) createRequire(entry).resolve(dep);const r=createRequire(process.argv[2]);if(r('./product-identity.cjs').productProfile('qq').productName!=='QQVibe')process.exit(2)"
        subprocess.run([shutil.which("node"), "-e", command, json.dumps(edges),
                        str(app_dir / "scripts/desktop-main.cjs")], check=True, timeout=15)

    def test_staged_qq_modules_import_without_the_checkout(self):
        actual = load("stage_qq_check", "stage-real-client.py")
        self.assertIn("qq_normalize.py", actual.BRIDGE)
        client = self.root / "isolated-qq-client"
        for name in actual.BRIDGE:
            self.put(client / "bridge" / name, (builder.ROOT / "bridge" / name).read_bytes())
        for name in ("product-identity.json", "product-identity.cjs"):
            self.put(client / "scripts" / name, (builder.ROOT / "scripts" / name).read_bytes())
        subprocess.run([sys.executable, "-I", "-c",
                        "import sys;sys.path.insert(0,sys.argv[1]);import qq_source,qq_normalize;assert qq_source.QQSource.kind=='qq'",
                        str(client / "bridge")], cwd=client, check=True, timeout=15)

    def test_root_allowlist_keeps_update_files(self):
        for name in ("real-client-update.cjs", "real-client-update-controller.cjs",
                     "real-client-update-helper.cjs", "real-client-update-extract.py",
                     "update-signing.pub"):
            self.assertIn(name, load("stage_real_client_check", "stage-real-client.py").SCRIPTS)
            self.assertTrue((builder.ROOT / "scripts" / name).is_file())

    def test_real_selection_backend_is_in_stage_allowlist(self):
        actual_stage = load("stage_selection_check", "stage-real-client.py")
        self.assertIn("conversation_selection.py", actual_stage.BRIDGE)
        self.assertTrue((builder.ROOT / "bridge/conversation_selection.py").is_file())

    def test_runtime_stage_refuses_nonempty_directory_without_replacing_it(self):
        stale = self.root / "stale-stage"
        self.put(stale / "keep.txt", b"keep")
        run = subprocess.run([sys.executable, str(builder.ROOT / "scripts/stage-real-runtime.py"),
                              "--output", str(stale)], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("runtime stage must be new and empty", run.stderr)
        self.assertEqual((stale / "keep.txt").read_bytes(), b"keep")

    def test_real_asar_api_checks_script_hashes_and_version(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        config = json.loads(builder.TEMPLATE.read_text(encoding="utf-8"))
        names = [name for name in config["files"] if name.startswith("scripts/")]
        app_dir = self.root / "synthetic-app"
        expected = {}
        for name in names:
            source = builder.ROOT / name
            self.put(app_dir / name, source.read_bytes())
            expected[name.casefold()] = self.row(app_dir, name)
        version = json.loads((builder.ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        self.put(app_dir / "package.json", json.dumps({
            "name": builder.current_product().product_name.lower(), "version": version, "main": "scripts/desktop-main.cjs",
        }).encode())
        unpacked = self.root / "synthetic-unpacked"
        archive = unpacked / "resources/app.asar"
        archive.parent.mkdir(parents=True)
        subprocess.run([node, "-e", "require('@electron/asar').createPackage(process.argv[1], process.argv[2]).catch(e=>{console.error(e);process.exit(1)})",
                        str(app_dir), str(archive)], cwd=builder.ROOT, check=True)
        builder.verify_asar(builder.ROOT, unpacked, Path(node), version, expected)
        expected[names[0].casefold()]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "app.asar script hash differs"):
            builder.verify_asar(builder.ROOT, unpacked, Path(node), version, expected)


if __name__ == "__main__":
    unittest.main()

"""Pinned-runtime selection never executes unverified binaries or overwrites data."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


prereq = load("prereq_test", "build-prerequisites.py")
builder = load("icon_builder_test", "build-product-icons.py")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.source = self.root / "existing/node.exe"
        self.source.parent.mkdir(); self.source.write_bytes(b"synthetic-node")
        self.license = self.source.parent / "LICENSE"; self.license.write_bytes(b"synthetic-license\n")
        (self.root / "licenses").mkdir(); (self.root / "licenses/node-LICENSE-24.11.1.txt").write_bytes(self.license.read_bytes())
        self.expected = prereq.digest(self.source)

    def test_hash_rejection_occurs_before_any_process_execution(self):
        with patch.object(prereq.subprocess, "check_output", side_effect=AssertionError("must not execute")):
            with self.assertRaisesRegex(ValueError, "official"):
                prereq.verify_node(self.source)

    def test_prepare_existing_verified_runtime_is_idempotent_and_path_unchanged(self):
        before = os.environ.get("PATH")
        with patch.object(prereq, "NODE_SHA256", self.expected), patch.object(prereq.subprocess, "check_output", return_value="v24.11.1\n"):
            first = prereq.prepare_node(self.source, self.license, self.root)
            second = prereq.prepare_node(self.source, self.license, self.root)
        self.assertEqual(first, second); self.assertEqual(os.environ.get("PATH"), before)
        self.assertEqual((self.root / prereq.LOCAL_NODE).read_bytes(), self.source.read_bytes())
        self.assertEqual(list((self.root / prereq.LOCAL_NODE).parent.glob("*.part-*")), [])

    def test_existing_wrong_output_is_preserved(self):
        target = self.root / prereq.LOCAL_NODE; target.parent.mkdir(parents=True); target.write_bytes(b"keep-this")
        with patch.object(prereq, "NODE_SHA256", self.expected), patch.object(prereq.subprocess, "check_output", return_value="v24.11.1\n"):
            with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                prereq.prepare_node(self.source, self.license, self.root)
        self.assertEqual(target.read_bytes(), b"keep-this")

    def test_license_mismatch_cannot_create_build_runtime(self):
        self.license.write_text("wrong-license")
        with patch.object(prereq, "NODE_SHA256", self.expected), patch.object(prereq.subprocess, "check_output", return_value="v24.11.1\n"):
            with self.assertRaisesRegex(ValueError, "license differs"):
                prereq.prepare_node(self.source, self.license, self.root)
        self.assertFalse((self.root / prereq.LOCAL_NODE).exists())

    def test_explicit_and_configured_runtime_take_precedence(self):
        with patch.object(prereq, "verify_node", side_effect=lambda value: Path(value)), patch.dict(os.environ, {"WECHATVIBE_BUILD_NODE": "configured.exe"}):
            self.assertEqual(prereq.selected_node("explicit.exe", self.root), Path("explicit.exe"))
            self.assertEqual(prereq.selected_node(None, self.root), Path("configured.exe"))

    def test_prepared_node_is_preferred_to_path_and_tampering_is_not_silently_bypassed(self):
        target = self.root / prereq.LOCAL_NODE; target.parent.mkdir(parents=True); target.write_bytes(self.source.read_bytes())
        with patch.dict(os.environ, {}, clear=True), patch.object(prereq, "NODE_SHA256", self.expected), patch.object(
                prereq.subprocess, "check_output", return_value="v24.11.1\n"), patch.object(prereq.shutil, "which", side_effect=AssertionError("must not fallback")):
            self.assertEqual(prereq.selected_node(root=self.root), target)
            target.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "official"): prereq.selected_node(root=self.root)


class IconTests(unittest.TestCase):
    def test_code_native_icon_and_windows_sizes_are_distinct_from_upstream(self):
        root = Path(__file__).resolve().parents[1]
        qq = root / "chatui/assets/qqvibe-icon.png"
        with Image.open(qq) as image:
            self.assertEqual(image.size, (1024, 1024)); self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        with Image.open(root / "chatui/assets/qqvibe-icon.ico") as image:
            self.assertTrue({(16, 16), (32, 32), (256, 256)} <= image.ico.sizes())
        self.assertNotEqual(qq.read_bytes(), (root / "chatui/assets/wechatvibe-icon.png").read_bytes())
        self.assertNotIn("href=", builder.svg()); self.assertNotIn("<image", builder.svg())

    def test_qq_package_and_lock_have_independent_preview_identity(self):
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "package.json").read_text())
        locked = json.loads((root / "package-lock.json").read_text())
        self.assertEqual(package["name"], "qqvibe")
        self.assertRegex(package["version"], r"^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$")
        self.assertEqual(package["name"], locked["name"])
        self.assertEqual(package["version"], locked["packages"][""]["version"])


if __name__ == "__main__": unittest.main()

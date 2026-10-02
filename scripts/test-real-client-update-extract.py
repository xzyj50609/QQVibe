"""Synthetic ZIP security checks for the update candidate extractor."""
from __future__ import annotations

import importlib.util
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("real-client-update-extract.py")
SPEC = importlib.util.spec_from_file_location("update_extract", SCRIPT)
extractor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extractor)


def standard_members():
    members = []
    for relative in extractor.REQUIRED_FILES:
        if relative == "WechatVibe.exe":
            payload = b"MZsynthetic"
        elif relative == "resources/client/package.json":
            payload = json.dumps({"name": "wechatvibe-runtime", "version": "1.0.2"}).encode()
        else:
            payload = b"synthetic"
        members.append(("win-unpacked/" + relative, payload))
    return members


class ExtractTests(unittest.TestCase):
    def make_archive(self, directory, extra=(), replace=None):
        archive = directory / "update.zip"
        members = standard_members() if replace is None else replace
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for name, payload in [*members, *extra]:
                output.writestr(name, payload)
        return archive

    def reject(self, extra=(), replace=None):
        with tempfile.TemporaryDirectory(prefix="wechatvibe-extract-test-") as folder:
            work = Path(folder)
            archive = self.make_archive(work, extra, replace)
            with self.assertRaises(ValueError):
                extractor.extract(archive, work, "1.0.2")

    def test_valid_candidate(self):
        with tempfile.TemporaryDirectory(prefix="wechatvibe-extract-test-") as folder:
            work = Path(folder)
            archive = self.make_archive(work)
            candidate = extractor.extract(archive, work, "1.0.2")
            self.assertEqual(candidate, work / "win-unpacked")
            self.assertEqual((candidate / "WechatVibe.exe").read_bytes(), b"MZsynthetic")
            self.assertFalse((candidate / ".local").exists())

    def test_unsafe_paths(self):
        for name in (
            "win-unpacked/../outside.txt", "/win-unpacked/absolute.txt",
            "win-unpacked/C:/drive.txt", "win-unpacked/file:stream",
            "win-unpacked/.local/state.db",
            "win-unpacked/CON.txt", "win-unpacked/trailing. ",
            "WechatVibe-1.0.2/WechatVibe.exe",
        ):
            with self.subTest(name=name):
                self.reject(extra=[(name, b"bad")])
        # zipfile normalizes backslashes while creating a test ZIP on Windows.
        with self.assertRaises(ValueError):
            extractor._parts("win-unpacked\\backslash.txt", False)

    def test_duplicates_and_file_parent(self):
        self.reject(extra=[("win-unpacked/wechatvibe.EXE", b"collision")])
        self.reject(extra=[("win-unpacked/WechatVibe.exe/subfile", b"collision")])

    def test_symlink_and_reparse(self):
        link = zipfile.ZipInfo("win-unpacked/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.reject(extra=[(link, b"target")])
        reparse = zipfile.ZipInfo("win-unpacked/reparse")
        reparse.create_system = 0
        reparse.external_attr = 0x400
        self.reject(extra=[(reparse, b"target")])

    def test_bomb_ratio_and_version(self):
        self.reject(extra=[("win-unpacked/zeros", b"\x00" * (2 * 1024 * 1024))])
        wrong = [(name, json.dumps({"name": "wechatvibe-runtime", "version": "1.0.3"}).encode()
                  if name.endswith("/package.json") else payload)
                 for name, payload in standard_members()]
        self.reject(replace=wrong)

    def test_each_required_file_missing_or_empty(self):
        for relative in extractor.REQUIRED_FILES:
            name = "win-unpacked/" + relative
            with self.subTest(missing=relative):
                self.reject(replace=[item for item in standard_members() if item[0] != name])
            with self.subTest(empty=relative):
                self.reject(replace=[(member, b"" if member == name else payload)
                                     for member, payload in standard_members()])


if __name__ == "__main__":
    unittest.main()

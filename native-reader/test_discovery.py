"""Synthetic WeChat 4.x data-root discovery compatibility checks."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wr import discovery


class _RegistryHandle:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.appdata = self.root / "roaming"
        self.localappdata = self.root / "local"
        self.home = self.root / "home"
        for path in (self.appdata, self.localappdata, self.home):
            path.mkdir()
        env = patch.dict(os.environ, {
            "APPDATA": str(self.appdata),
            "LOCALAPPDATA": str(self.localappdata),
            "USERPROFILE": str(self.home),
        })
        env.start()
        self.addCleanup(env.stop)
        self.registry_values = {}

        def open_key(_hive, name):
            if name not in self.registry_values:
                raise OSError("missing synthetic registry key")
            return _RegistryHandle(name)

        def enum_value(handle, index):
            try:
                return self.registry_values[handle.name][index]
            except IndexError:
                raise OSError("end of synthetic registry values") from None

        fake_registry = SimpleNamespace(
            HKEY_CURRENT_USER=object(), OpenKey=open_key, EnumValue=enum_value
        )
        registry = patch.dict(sys.modules, {"winreg": fake_registry})
        registry.start()
        self.addCleanup(registry.stop)

    def _account(self, base, layout, name):
        account = base / layout / name if layout else base / name
        (account / "db_storage").mkdir(parents=True)
        return account / "db_storage"

    def _discovered(self):
        return {Path(account.path) for account in discovery.discover_account_dirs()}

    def test_six_config_locations_json_plain_path_and_legacy_gbk(self):
        cases = [
            (self.appdata, ("xwechat",), "json.json", "xwechat_files", "json", "dataDir"),
            (self.appdata, ("xwechat", "config"), "path.ini", "WeChat Files", "ini", None),
            (self.appdata, ("WeChat",), "location.cfg", "xwechat_files_data", "plain", None),
            (self.localappdata, ("xwechat",), "config.txt", "", "json", "fileSavePath"),
            (self.localappdata, ("xwechat", "config"), "paths.json", "xwechat_files", "list", None),
            (self.localappdata, ("WeChat",), "legacy.ini", "WeChat Files", "gbk", None),
        ]
        expected = set()
        for index, (appdata, parts, filename, layout, fmt, field) in enumerate(cases):
            base = self.root / ("外部微信数据" if fmt == "gbk" else f"external-{index}")
            expected.add(self._account(base, layout, f"account-{index}"))
            config_dir = appdata.joinpath("Tencent", *parts)
            config_dir.mkdir(parents=True, exist_ok=True)
            config_file = config_dir / filename
            if fmt == "json":
                content = json.dumps({field: str(base)}, ensure_ascii=False).encode("utf-8")
            elif fmt == "list":
                content = json.dumps([str(base)]).encode("utf-8")
            elif fmt == "ini":
                content = f"fileSavePath={base}".encode("utf-8-sig")
            elif fmt == "gbk":
                content = str(base).encode("gbk")
            else:
                content = str(base).encode("utf-16")
            config_file.write_bytes(content)
        self.assertEqual(self._discovered(), expected)

    def test_all_upstream_json_path_keys_and_default_roots(self):
        base = self.root / "configured"
        self._account(base, "xwechat_files_data", "custom")
        for key in discovery.CONFIG_PATH_KEYS:
            with self.subTest(key=key):
                content = json.dumps({key: str(base)})
                self.assertEqual(discovery._config_content_directories(content), [str(base)])
        self.assertEqual(discovery._config_content_directories(
            json.dumps({"unrelatedPath": str(base)})), [])
        documents = self.home / "Documents"
        default = self._account(documents, "WeChat Files", "default")
        home_default = self._account(self.home, "xwechat_files", "home-default")
        self.assertEqual(self._discovered(), {default, home_default})
        self.assertTrue(all(os.path.isabs(root) and os.path.isdir(root)
                            for root in discovery._known_roots()))

    def test_named_and_legacy_registry_values_and_live_owner_confirmation(self):
        base = self.root / "registry-root"
        expected = self._account(base, "WeChat Files", "registered")
        legacy_base = self.root / "xwechat-legacy-root"
        legacy = self._account(legacy_base, "xwechat_files", "legacy")
        decoy_base = self.root / "decoy"
        self._account(decoy_base, "xwechat_files", "wrong")
        self.registry_values[r"Software\Tencent\xwechat\config"] = [
            ("InstallLocation", str(decoy_base), 1),
            ("DataDir", "relative-path", 1),
            ("FileSavePath", str(base), 1),
        ]
        self.registry_values[r"Software\Tencent\Weixin"] = [
            ("LegacyValue", str(legacy_base), 1),
        ]
        accounts = discovery.discover_account_dirs()
        self.assertEqual({Path(account.path) for account in accounts}, {expected, legacy})
        process = discovery.WeixinProcess(pid=42)
        with patch.object(discovery, "process_open_file_paths", return_value=[]):
            self.assertIsNone(discovery.active_account_id(accounts, [process]))
        with patch.object(discovery, "process_open_file_paths",
                          return_value=[str(expected / "session" / "session.db")]):
            selected = next(account for account in accounts if Path(account.path) == expected)
            self.assertEqual(discovery.active_account_id(accounts, [process]), selected.id)

    def test_only_sorted_config_files_are_read(self):
        config_dir = self.appdata / "Tencent" / "WeChat"
        config_dir.mkdir(parents=True)
        decoy = self.root / "binary-decoy"
        self._account(decoy, "xwechat_files", "wrong")
        (config_dir / "secret.db").write_text(str(decoy), encoding="utf-8")
        (config_dir / "bridge.log").write_text(str(decoy), encoding="utf-8")
        for index in range(discovery.MAX_CONFIG_ENTRIES_PER_DIR):
            (config_dir / f"z{index:02}.ini").write_text("relative-path", encoding="utf-8")
        expected_base = self.root / "configured-first"
        expected = self._account(expected_base, "WeChat Files", "found")
        # Created last, but its name sorts before the 64 irrelevant .ini files.
        (config_dir / "a-first.ini").write_text(str(expected_base), encoding="utf-8")
        self.assertEqual(self._discovered(), {expected})

    def test_config_read_is_bounded(self):
        base = self.root / "outside-default-roots"
        self._account(base, "xwechat_files", "unseen")
        config_dir = self.appdata / "Tencent" / "xwechat" / "config"
        config_dir.mkdir(parents=True)
        tail = json.dumps({"dataDir": str(base)}).encode("utf-8")
        (config_dir / "oversized.ini").write_bytes(b"#" * discovery.CONFIG_READ_LIMIT + tail)
        self.assertEqual(self._discovered(), set())


if __name__ == "__main__":
    unittest.main()

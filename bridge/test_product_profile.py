"""Product identity is one manifest; the QQ client must not inherit WeChat paths or feeds."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import product_profile as profile


class ManifestTests(unittest.TestCase):
    def test_every_named_product_is_isolated_from_the_other(self):
        wechat = profile.load_product("wechat")
        qq = profile.load_product("qq")
        for field in ("product_name", "app_id", "data_dir", "instance_salt",
                      "control_token_env", "control_token_header", "storage_namespace", "icon_png", "icon_ico"):
            self.assertNotEqual(getattr(wechat, field), getattr(qq, field), field)

    def test_this_repository_defaults_to_the_qq_product(self):
        with patch.dict(profile.os.environ, {}, clear=True):
            self.assertEqual(profile.current_product().key, "qq")

    def test_environment_selects_the_product(self):
        with patch.dict(profile.os.environ, {profile.PRODUCT_ENV: "wechat"}):
            self.assertEqual(profile.current_product().key, "wechat")

    def test_unknown_product_is_rejected(self):
        with self.assertRaises(ValueError):
            profile.load_product("tim")

    def test_icon_path_must_be_a_local_asset_without_traversal(self):
        import copy
        for value in ("../private.ico", "C:/private.ico", "chatui/assets/../../private.ico", "https://remote/icon.ico"):
            document = copy.deepcopy(profile._manifest())
            document["products"]["bad-icon"] = {**document["products"]["qq"], "iconIco": value}
            with self.subTest(value=value), patch.object(profile, "_manifest", return_value=document):
                with self.assertRaisesRegex(ValueError, "icon path"): profile.load_product("bad-icon")

    def test_wechat_state_paths_are_unchanged(self):
        """The original product keeps its own layout; only QQ moves to a new data root."""
        with TemporaryDirectory() as root:
            wechat = profile.load_product("wechat")
            self.assertEqual(wechat.state_dir("real-client-data", Path(root)),
                             Path(root) / ".local" / "real-client-data")

    def test_qq_state_paths_never_reuse_the_wechat_directory(self):
        with TemporaryDirectory() as root:
            qq = profile.load_product("qq")
            data_root = qq.data_root(Path(root))
            self.assertEqual(data_root, Path(root) / "QQVibeData")
            self.assertEqual(qq.state_dir("real-client-data", Path(root)),
                             data_root / "real-client-data")
            self.assertNotEqual(data_root, Path(root) / ".local")


class UpdateChannelTests(unittest.TestCase):
    def test_qq_has_no_configured_update_source(self):
        qq = profile.load_product("qq")
        self.assertIsNone(qq.update_repository)
        self.assertFalse(qq.update_channel_enabled)

    def test_wechat_keeps_its_upstream_feed(self):
        wechat = profile.load_product("wechat")
        self.assertTrue(wechat.update_channel_enabled)
        self.assertNotEqual(wechat.update_repository, profile.load_product("qq").update_repository)


class InstanceIsolationTests(unittest.TestCase):
    def test_two_products_on_one_checkout_never_share_a_bridge(self):
        import instance_identity

        root = Path.cwd()
        wechat_id = instance_identity.instance_id(root, "wechat")
        qq_id = instance_identity.instance_id(root, "qq")
        self.assertNotEqual(wechat_id, qq_id)
        self.assertNotEqual(instance_identity.default_port(root, "wechat"),
                            instance_identity.default_port(root, "qq"))

    def test_bridge_answers_only_its_own_product_token_channel(self):
        import real_http

        self.assertEqual(real_http.CONTROL_TOKEN_ENV, "QQVIBE_CONTROL_TOKEN")
        self.assertEqual(real_http.CONTROL_TOKEN_HEADER, "X-QQVibe-Control-Token")


class AccountDirectoryTests(unittest.TestCase):
    def test_directory_name_is_the_bare_hex_of_the_account_key(self):
        key = "a:" + "ab" * 16
        with TemporaryDirectory() as root:
            path = profile.load_product("qq").account_directory(key, Path(root))
            self.assertEqual(path, Path(root) / "QQVibeData" / "accounts" / ("ab" * 16))
            self.assertNotIn(":", path.name)

    def test_keys_that_are_not_the_account_hash_are_rejected(self):
        with TemporaryDirectory() as root:
            qq = profile.load_product("qq")
            for key in ("ab" * 16, "a:ZZZ", "a:" + "ab" * 15, "u:peer", "", None):
                with self.subTest(key=key):
                    with self.assertRaises(ValueError):
                        qq.account_directory(key, Path(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Synthetic tests for the pure per-message and portrait contract modules."""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

import backend_contracts
import message_contracts
import portrait_contracts

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
CONTRACT_MODULES = ("message_contracts", "portrait_contracts")


def parsed(name):
    return ast.parse((BRIDGE / (name + ".py")).read_text(encoding="utf-8"))


def imports(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


class ConstantIdentityTests(unittest.TestCase):
    def test_backend_contracts_reexports_the_same_constant_objects(self):
        self.assertIs(backend_contracts.FINE_LABEL_SCHEMA, message_contracts.FINE_LABEL_SCHEMA)
        self.assertIs(backend_contracts.API_INSIGHT_REVISION, message_contracts.API_INSIGHT_REVISION)
        self.assertIs(backend_contracts.API_PORTRAIT_REVISION, portrait_contracts.API_PORTRAIT_REVISION)
        self.assertIs(backend_contracts.api_insight_scope, message_contracts.api_insight_scope)
        self.assertIs(backend_contracts.api_portrait_scope, portrait_contracts.api_portrait_scope)

    def test_constant_values_and_cache_keys_are_unchanged(self):
        self.assertEqual(message_contracts.FINE_LABEL_SCHEMA, "generic-v9")
        self.assertEqual(message_contracts.API_INSIGHT_REVISION, "free-label-v5-simple")
        self.assertEqual(portrait_contracts.API_PORTRAIT_REVISION, "portrait-v2")
        self.assertEqual(message_contracts.api_insight_scope("api:one"), "api:one:free-label-v5-simple")
        self.assertEqual(portrait_contracts.api_portrait_scope("api:one"), "api:one:portrait-v2")
        self.assertEqual(backend_contracts.api_insight_scope("api:one"), "api:one:free-label-v5-simple")
        self.assertEqual(backend_contracts.api_portrait_scope("api:one"), "api:one:portrait-v2")

    def test_fine_revision_change_does_not_move_the_portrait_scope(self):
        original = message_contracts.API_INSIGHT_REVISION
        try:
            message_contracts.API_INSIGHT_REVISION = "free-label-v5-simple"
            self.assertEqual(message_contracts.api_insight_scope("api:one"), "api:one:free-label-v5-simple")
            # The portrait scope reads its own module constant and is unaffected.
            self.assertEqual(portrait_contracts.api_portrait_scope("api:one"), "api:one:portrait-v2")
            self.assertEqual(backend_contracts.api_portrait_scope("api:one"), "api:one:portrait-v2")
        finally:
            message_contracts.API_INSIGHT_REVISION = original


class PurityTests(unittest.TestCase):
    def test_contract_modules_depend_only_on_the_standard_library(self):
        for name in CONTRACT_MODULES:
            with self.subTest(module=name):
                self.assertFalse(imports(parsed(name)) - sys.stdlib_module_names - {"__future__"})

    def test_contract_modules_do_not_import_each_other_or_any_service(self):
        for name in CONTRACT_MODULES:
            with self.subTest(module=name):
                found = imports(parsed(name))
                self.assertFalse(found & {
                    "backend_contracts", "backend_service", "real_backend", "real_http",
                    "result_store", "node_analysis", "wechat_source", "model_source", "batch_engine",
                })
        self.assertNotIn("portrait_contracts", imports(parsed("message_contracts")))
        self.assertNotIn("message_contracts", imports(parsed("portrait_contracts")))


if __name__ == "__main__":
    unittest.main()

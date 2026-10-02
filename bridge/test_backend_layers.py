"""Synthetic regression tests for the backend service/repository boundary."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import backend_contracts
import backend_service
import model_source
import node_analysis
import real_backend
import result_store
import wechat_source
from batch_engine import BatchEngine
from batch_state import BatchStateStore
from profile_state import empty_state

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
LAYER_MODULES = ("backend_contracts", "backend_service", "message_results", "message_contracts",
                 "message_input", "portrait_contracts", "api_tasks", "node_analysis",
                 "result_store", "wechat_source")


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


class LayerBoundaryTests(unittest.TestCase):
    def test_legacy_facade_preserves_class_and_exception_identity(self):
        for owner, names in (
            (backend_service, ("Backend",)),
            (result_store, ("ResultStore",)),
            (wechat_source, ("WeChatSource", "message_id", "avatar_candidates")),
            (node_analysis, ("NodeAnalysis",)),
            (backend_contracts, ("ForecastRequestError", "AccountChangedError",
                                 "AccountUnavailableError", "MessagesUnavailableError", "ModelSourceUnavailable",
                                 "valid_api_portrait", "affinity", "infer_mbti")),
        ):
            for name in names:
                with self.subTest(name=name):
                    self.assertIs(getattr(real_backend, name), getattr(owner, name))
        self.assertIs(model_source.ModelSourceUnavailable, backend_contracts.ModelSourceUnavailable)
        self.assertEqual(model_source.LOCAL_SOURCE_ID, backend_contracts.LOCAL_SOURCE_ID)
        self.assertTrue(all(hasattr(real_backend, name) for name in real_backend.__all__))

    def test_facade_contains_no_business_implementation(self):
        self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                             for node in ast.walk(parsed("real_backend"))))

    def test_contracts_have_no_application_layer_dependencies(self):
        self.assertFalse(imports(parsed("backend_contracts")) - sys.stdlib_module_names -
                         {"__future__", "message_contracts", "portrait_contracts"})
        for name in ("message_contracts", "portrait_contracts"):
            with self.subTest(module=name):
                self.assertFalse(imports(parsed(name)) - sys.stdlib_module_names - {"__future__"})

    def test_repositories_and_adapters_never_import_services_or_facade(self):
        for name in ("backend_contracts", "message_results", "result_store", "batch_state",
                     "wechat_source", "node_analysis"):
            with self.subTest(module=name):
                self.assertFalse(imports(parsed(name)) & {"real_backend", "backend_service", "real_http", "batch_engine"})
        self.assertFalse(imports(parsed("result_store")) & {"wechat_source", "node_analysis", "model_source"})

    def test_message_results_depends_only_on_contracts_and_profile_signals(self):
        found = imports(parsed("message_results"))
        self.assertTrue(found <= sys.stdlib_module_names | {"__future__", "backend_contracts", "profile_signals"})
        self.assertFalse(found & {"real_backend", "backend_service", "real_http", "batch_engine",
                                  "result_store", "node_analysis", "wechat_source", "model_source"})

    def test_message_input_depends_only_on_the_standard_library(self):
        found = imports(parsed("message_input"))
        self.assertTrue(found <= sys.stdlib_module_names | {"__future__"})
        self.assertFalse(found & {"backend_service", "real_backend", "backend_contracts",
                                  "node_analysis", "wechat_source", "model_source", "batch_engine",
                                  "result_store", "message_contracts", "portrait_contracts"})

    def test_api_tasks_depends_only_on_the_standard_library(self):
        found = imports(parsed("api_tasks"))
        self.assertTrue(found <= sys.stdlib_module_names | {"__future__"})
        self.assertFalse(found & {"backend_service", "real_backend", "result_store",
                                  "node_analysis", "wechat_source", "model_source", "batch_engine"})

    def test_message_results_exposes_only_the_two_pure_validators(self):
        import message_results
        self.assertTrue(callable(message_results.validate_fine_result))
        self.assertTrue(callable(message_results.validate_portrait_result))
        self.assertNotIn("Backend", vars(message_results))

    def test_services_issue_no_sql_or_raw_database_connections(self):
        for name in ("backend_service", "batch_engine"):
            tree = parsed(name)
            with self.subTest(module=name):
                self.assertNotIn("sqlite3", imports(tree))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, {"connect", "execute", "executemany", "executescript"})
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotRegex(node.value, r"(?i)\b(?:SELECT .+ FROM|INSERT INTO|UPDATE \w+ SET|CREATE TABLE|DELETE FROM)\b")

    def test_layer_imports_have_no_process_database_or_network_side_effects(self):
        code = """
import importlib, socket, sqlite3, subprocess, sys, threading
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
with patch.object(sqlite3, 'connect', side_effect=AssertionError('database on import')), \
     patch.object(subprocess, 'Popen', side_effect=AssertionError('process on import')), \
     patch.object(threading.Thread, 'start', side_effect=AssertionError('thread on import')), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network on import')):
    for name in ('result_store', 'node_analysis', 'wechat_source', 'backend_service', 'real_backend', 'message_results', 'message_contracts', 'portrait_contracts', 'api_tasks'):
        importlib.import_module(name)
print('IMPORTS_HAVE_NO_RUNTIME_SIDE_EFFECTS')
"""
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(BRIDGE)],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("IMPORTS_HAVE_NO_RUNTIME_SIDE_EFFECTS", completed.stdout)

    def test_packaged_bridge_includes_and_imports_all_layers_without_source_tree(self):
        spec = importlib.util.spec_from_file_location("stage_for_layer_test", ROOT / "scripts/stage-real-client.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        self.assertTrue({name + ".py" for name in LAYER_MODULES} <= set(stage.BRIDGE))
        self.assertIn("product-identity.json", stage.SCRIPTS)
        with tempfile.TemporaryDirectory(prefix="wechatvibe-layer-stage-") as temporary:
            target = Path(temporary) / "client" / "bridge"
            target.mkdir(parents=True)
            # The bridge reads the product manifest next to the staged scripts.
            staged_scripts = target.parent / "scripts"
            staged_scripts.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "scripts/product-identity.json",
                         staged_scripts / "product-identity.json")
            for name in stage.BRIDGE:
                if name.endswith(".py"):
                    shutil.copy2(BRIDGE / name, target / name)
            code = """
import importlib, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import chat_server, real_http, real_backend
for name in ('backend_contracts', 'backend_service', 'message_results', 'message_contracts', 'message_input', 'portrait_contracts', 'api_tasks', 'result_store', 'node_analysis', 'wechat_source'):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().parent == root, name
assert real_http.Backend is real_backend.Backend
assert real_http.WeChatSource is real_backend.WeChatSource
print('STAGED_BRIDGE_IMPORTS_OK')
"""
            completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(target)],
                                       cwd=temporary, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("STAGED_BRIDGE_IMPORTS_OK", completed.stdout)


class MessageInputBoundaryTests(unittest.TestCase):
    def test_analyze_item_attaches_trusted_scope_without_changing_legacy_fields(self):
        captured = {}

        class Analyzer:
            def analyze(self, session, messages, target, **_kwargs):
                captured["messages"] = messages
                return {}

        backend = backend_service.Backend.__new__(backend_service.Backend)
        backend.analyzer = Analyzer()
        backend.source_kind = "wechat"
        store = Mock()
        store.path = "synthetic.sqlite3"
        item = {"id": "m1", "side": "other", "kind": "text", "text": "hi",
                "senderId": "member-a", "senderName": "阿甲", "time": 1.5}
        with self.assertRaises(RuntimeError):
            backend._analyze_item("acct", "friend", "synthetic-version", store, [item], item,
                                  defer=True)
        [prepared] = captured["messages"]
        self.assertEqual({key: prepared[key] for key in item}, item)
        self.assertNotIn("inputMeta", item)
        meta = prepared["inputMeta"]
        self.assertEqual((meta["accountId"], meta["conversationId"]), ("acct", "friend"))
        self.assertEqual((meta["senderId"], meta["senderName"]), ("member-a", "阿甲"))
        self.assertEqual(meta["sentAtMs"], 1.5)
        self.assertEqual(meta["source"]["kind"], "wechat")
        store.save.assert_not_called()
        store.save_fine.assert_not_called()

    def test_a_source_declares_its_own_kind_and_is_never_assumed_wechat(self):
        """C09: QQ records must not pass validation disguised as wechat/ocr/unknown."""
        class Declared:
            kind = "qq"

        class Undeclared:
            pass

        class Disguised:
            kind = "camera"

        self.assertEqual(backend_service.trusted_source_kind(Declared()), "qq")
        self.assertEqual(backend_service.trusted_source_kind(wechat_source.WeChatSource()), "wechat")
        self.assertEqual(backend_service.trusted_source_kind(Undeclared()), "unknown")
        self.assertEqual(backend_service.trusted_source_kind(Disguised()), "unknown")


class RepositoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechatvibe-repository-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = result_store.ResultStore(self.root / "synthetic.sqlite3")
        self.batches = BatchStateStore(self.store)
        self.scope = ("synthetic-account", "synthetic-friend", "synthetic-version")
        self.subject = "synthetic-friend"

    @staticmethod
    def message(stable_id, number):
        return {"id": stable_id, "_sort": (number, "message__message_0.db", number),
                "senderId": "synthetic-friend", "side": "other", "kind": "text", "text": "synthetic"}

    def save(self, stable_id, number, scope=None):
        with patch("result_store.keyword_counts", return_value={}):
            self.store.save(*(scope or self.scope), self.message(stable_id, number), {"score": None})

    def test_project_factory_preserves_account_hashed_path_and_ignores_workdir(self):
        with patch.object(result_store, "ROOT", self.root):
            one = result_store.project_result_store(self.scope[0], self.root / "snapshot-one")
            two = result_store.project_result_store(self.scope[0], self.root / "snapshot-two")
            other = result_store.project_result_store("other-account", self.root / "snapshot-one")
        expected = result_store.PRODUCT.state_dir("real-client-data", self.root) / (
            hashlib.sha256(self.scope[0].encode()).hexdigest() + ".sqlite3")
        self.assertEqual(one.path, expected)
        self.assertEqual(two.path, expected)
        self.assertNotEqual(other.path, expected)

    def test_first_skip_is_ordered_and_account_session_version_scoped(self):
        self.assertIsNone(self.store.first_skipped_position(*self.scope))
        for index in (8, 2, 5):
            self.store.skip(*self.scope, self.message(str(index), index), "synthetic-skip")
        self.assertEqual(self.store.first_skipped_position(*self.scope), (2, "message__message_0.db", 2))
        for scope in (("other-account", *self.scope[1:]), (self.scope[0], "other-session", self.scope[2]),
                      (*self.scope[:2], "other-version")):
            self.assertIsNone(self.store.first_skipped_position(*scope))

    def test_repository_connection_rolls_back_on_error_and_commits_on_success(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic abort"):
            with self.store.connect() as conn:
                conn.execute("INSERT INTO analysis_skips VALUES (?,?,?,?,?,?,?,?)",
                             (*self.scope[:2], "rolled-back", self.scope[2], 1, "message__message_0.db", 1, "skip"))
                raise RuntimeError("synthetic abort")
        self.assertIsNone(self.store.first_skipped_position(*self.scope))
        self.store.skip(*self.scope, self.message("committed", 2), "skip")
        reopened = result_store.ResultStore(self.store.path)
        self.assertEqual(reopened.first_skipped_position(*self.scope), (2, "message__message_0.db", 2))

    def test_legacy_known_requires_seed_but_empty_lookup_is_harmless(self):
        self.assertEqual(self.batches.legacy_known(*self.scope, self.subject, []), set())
        with self.assertRaisesRegex(RuntimeError, "seeded"):
            self.batches.legacy_known(*self.scope, self.subject, ["not-seeded"])

    def test_legacy_boundary_remains_frozen_across_reopen_and_reseed(self):
        self.save("before-seed", 1)
        self.batches.seed(*self.scope, self.subject, empty_state())
        self.save("after-seed", 2)
        ids = ["before-seed", "after-seed"]
        self.assertEqual(self.batches.legacy_known(*self.scope, self.subject, ids), {"before-seed"})
        reopened = BatchStateStore(result_store.ResultStore(self.store.path))
        reopened.seed(*self.scope, self.subject, empty_state())
        self.assertEqual(reopened.legacy_known(*self.scope, self.subject, ids), {"before-seed"})

    def test_legacy_known_respects_account_session_version_and_subject_scope(self):
        self.save("shared-id", 1)
        self.batches.seed(*self.scope, self.subject, empty_state())
        scopes = [("other-account", *self.scope[1:]), (self.scope[0], "other-session", self.scope[2]),
                  (*self.scope[:2], "other-version")]
        for scope in scopes:
            self.batches.seed(*scope, self.subject, empty_state())
            self.assertEqual(self.batches.legacy_known(*scope, self.subject, ["shared-id"]), set())
        self.batches.seed(*self.scope, "other-member", empty_state())
        self.assertEqual(self.batches.legacy_known(*self.scope, "other-member", ["shared-id"]), set())

    def test_cursor_move_preserves_state_and_other_scopes_without_creating_results(self):
        seeded = self.batches.seed(*self.scope, self.subject, empty_state())
        self.batches.seed(*self.scope, "other-member", empty_state())
        self.batches.mark_complete(*self.scope, self.subject)
        position = (4, "message__message_0.db", 4)
        context = [{"id": "context", "side": "self", "text": "synthetic"}]
        self.batches.move_cursor(*self.scope, self.subject, position, context, char_offset=7)
        moved = self.batches.load(*self.scope, self.subject)
        self.assertEqual((moved["cursor"], moved["charOffset"], moved["context"]), (position, 7, context))
        self.assertEqual(moved["state"], seeded["state"])
        self.assertFalse(moved["complete"])
        self.assertIsNone(self.batches.load(*self.scope, "other-member")["cursor"])
        self.assertEqual(self.store.items(*self.scope), [])
        self.assertEqual(self.batches.outcomes(*self.scope, self.subject), {})
        self.batches.move_cursor(*self.scope, self.subject, None, [])
        self.assertEqual(self.batches.load(*self.scope, self.subject), moved)
        self.batches.move_cursor(*self.scope, self.subject, (5, position[1], 5), [])
        self.assertEqual(self.batches.load(*self.scope, self.subject)["charOffset"], 0)

    def test_batch_service_delegates_lookups_and_cursor_writes_to_repository(self):
        repository = Mock()
        repository.legacy_known.return_value = {"legacy-id"}
        repository.outcomes.return_value = {"batch-id": {"state": "done"}}
        engine = BatchEngine(object())
        # No connect(), path, SQL cursor, or even concrete ResultStore is exposed.
        fake_store = object()
        engine.store = Mock(return_value=repository)
        items = [self.message("legacy-id", 1), self.message("batch-id", 2)]
        self.assertEqual(engine.known(*self.scope, fake_store, self.subject, items),
                         {"legacy-id", "batch-id"})
        repository.legacy_known.assert_called_once_with(*self.scope, self.subject,
                                                        ["legacy-id", "batch-id"])
        position = (2, "message__message_0.db", 2)
        engine.move(*self.scope, fake_store, self.subject, position, [])
        repository.move_cursor.assert_called_once_with(*self.scope, self.subject, position, [])
        engine.store.reset_mock()
        engine.move(*self.scope, fake_store, self.subject, None, [])
        engine.store.assert_not_called()

    def test_reopened_store_keeps_progress_and_api_cache_isolation(self):
        self.save("local-result", 1)
        position = self.message("local-result", 1)["_sort"]
        self.store.advance(*self.scope, position, [], complete=True)
        message = self.message("api-result", 2)
        self.store.save_api_insights(*self.scope[:2], "api:one", [(message, {"summary": "synthetic"})])
        reopened = result_store.ResultStore(self.store.path)
        self.assertTrue(reopened.has(*self.scope, "local-result"))
        self.assertTrue(reopened.progress(*self.scope)["complete"])
        self.assertEqual(reopened.progress(*self.scope)["cursor"], position)
        self.assertEqual(reopened.api_insight_view(*self.scope[:2], "api:one"), {"api-result": {"summary": "synthetic"}})
        self.assertEqual(reopened.api_insight_view(*self.scope[:2], "api:two"), {})
        self.assertEqual(reopened.api_insight_view("other-account", self.scope[1], "api:one"), {})


if __name__ == "__main__":
    unittest.main()

"""Synthetic old bundled model to lite update preservation check."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import model_bundle
from local_model_source import ModelSource


class ModelPreserveTests(unittest.TestCase):
    def test_old_bundled_model_is_reused_by_lite_candidate(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        with tempfile.TemporaryDirectory(prefix="wechatvibe-model-preserve-") as temporary:
            root = Path(temporary)
            old = root / "old"
            candidate = root / "candidate"
            old_model = old / "resources/client/.models/laya"
            client = candidate / "resources/client"
            pins = {}
            for name in ("model.onnx", "onnx_config.json", "README.md",
                         "rl_agent_config.json", "tokenizer/tokenizer_config.json",
                         "tokenizer/tokenizer.json"):
                payload = ("pinned " + name).encode("utf-8")
                source = old_model / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(payload)
                pins[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            manifest = client / "scripts/model-files.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"schema": 1, "files": pins}), encoding="utf-8")
            # The staged client names its own local data directory in this manifest.
            shutil.copy2(ROOT / "scripts/product-identity.json",
                         client / "scripts/product-identity.json")
            helper = ROOT / "scripts/real-client-update-helper.cjs"
            command = ('const { preserveBundledModel } = require(process.argv[1]); '
                       'process.stdout.write(JSON.stringify(preserveBundledModel('
                       'process.argv[2], process.argv[3])));')
            result = subprocess.run([node, "-e", command, str(helper), str(old), str(candidate)],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, "true")
            self.assertFalse((client / ".models").exists())
            preserved = client / "QQVibeData/models/laya"
            for name, expected in pins.items():
                self.assertEqual(hashlib.sha256((preserved / name).read_bytes()).hexdigest(),
                                 expected["sha256"])
            with mock.patch.object(model_bundle, "MANIFEST", manifest):
                self.assertEqual(ModelSource(client).status(),
                                 {"state": "ready", "source": "downloaded", "path": str(preserved)})


if __name__ == "__main__":
    unittest.main()

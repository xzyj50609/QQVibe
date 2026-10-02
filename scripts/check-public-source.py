"""Verify immutable public snapshot inventory, staging inputs, imports and local doc links."""
from __future__ import annotations
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

def check(root):
    root = root.resolve()
    manifest = json.loads((root / "PUBLIC_SOURCE_MANIFEST.json").read_text(encoding="utf8"))
    expected = {row["file"]: row for row in manifest["files"]}
    actual = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()
              and not any(x in {".git", "node_modules", ".venv", "QQVibeData", ".local", "__pycache__"} for x in p.relative_to(root).parts)}
    assert set(actual) == set(expected) | {"PUBLIC_SOURCE_MANIFEST.json"}, "snapshot inventory mismatch"
    for name, row in expected.items():
        data = actual[name].read_bytes()
        assert len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"], name
    tree = ast.parse((root / "scripts/stage-real-client.py").read_text(encoding="utf8"))
    lists = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
            if key in {"SCRIPTS", "BRIDGE", "NATIVE_READER", "LAYA", "PUBLIC_FILES"}:
                lists[key] = ast.literal_eval(node.value)
    needed = list(lists["PUBLIC_FILES"])
    for key, base in [("SCRIPTS", "scripts"), ("BRIDGE", "bridge"), ("NATIVE_READER", "native-reader/wr"), ("LAYA", "electron/laya")]:
        needed += [base + "/" + name for name in lists[key]]
    needed += ["package.json", "package-lock.json", "python-requirements.lock.txt", "electron-builder.real-client.json", "tsconfig.electron.json"]
    for name in needed: assert (root / name).is_file(), "missing build input: " + name
    checked_imports = 0
    for p in actual.values():
        if p.suffix in {".ts", ".js", ".cjs", ".mjs"}:
            text = p.read_text(encoding="utf8")
            for target in re.findall(r"(?:from\s+|require\()['\"](\.[^'\"]+)['\"]", text):
                q = p.parent / target
                candidates = [q, *[Path(str(q) + s) for s in (".ts", ".js", ".cjs", ".mjs")], q / "index.ts", q / "index.js"]
                # Runtime node_modules is installed from the lock, not exported.
                if "node_modules" in q.parts: continue
                assert any(x.is_file() for x in candidates), "missing local import: " + p.relative_to(root).as_posix() + " -> " + target
                checked_imports += 1
    broken = []
    for p in [root / "README.md", *[x for x in actual.values() if x.suffix == ".md"]]:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", p.read_text(encoding="utf8")):
            if re.match(r"^(?:https?://|mailto:|#)", target): continue
            local = target.split("#")[0]
            # The original upstream notice is retained verbatim. Its links were
            # authored relative to the original repository root, not the archive.
            base = root if p.relative_to(root).as_posix() == "docs/migration/upstream/WechatVibe-1.2.2-THIRD_PARTY_NOTICES.md" else p.parent
            if local and not (base / local).exists(): broken.append({"file": p.relative_to(root).as_posix(), "target": target})
    assert not broken, "broken document links: " + json.dumps(broken[:20], ensure_ascii=False)
    return {"sourceCommit": manifest["sourceCommit"], "verifiedFiles": len(expected),
            "requiredBuildInputs": len(needed), "localImportsChecked": checked_imports,
            "documentLinksChecked": True, "validationPassed": True,
            "crossMachineAccepted": False}

if __name__ == "__main__": print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))

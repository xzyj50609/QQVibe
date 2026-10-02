"""Read-only dependency, source-origin and package-input audit. No release approval.

Run under the project Python. Output must be a new directory. Paths and hashes
are recorded, never suspect text, keys or private chat content.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "02b770821a1cf46b8ecc46b4f874ac6f922bd586"
LICENSE_NAMES = ("license", "licence", "copying", "notice", "copyright")
PRIVATE_DIRS = {".local", "qqvibedata", ".git", ".venv", "accounts", "screenshots", "secrets"}
PRIVATE_NAMES = {".env", "auth.json", "credentials.json", "token.json", "tokens.json", "keys.json"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".key", ".p12", ".pfx", ".pem", ".dmp"}
SECRET_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "literal-api-key": re.compile(rb"['\"]sk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}['\"]"),
    "literal-github-token": re.compile(rb"\b(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{70,})\b"),
}


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(path: Path, relative: str) -> dict:
    return {"file": relative, "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def is_license(path: Path) -> bool:
    return any(name in path.name.casefold() for name in LICENSE_NAMES)


def private_path(relative: str) -> bool:
    path = Path(relative)
    return (bool({p.casefold() for p in path.parts} & PRIVATE_DIRS) or
            path.name.casefold() in PRIVATE_NAMES or path.name.casefold().startswith(".env.") or
            path.suffix.casefold() in PRIVATE_SUFFIXES or
            path.name.casefold().endswith((".db-wal", ".db-shm")))


def source_origin(previous: bytes, current: bytes, name: str) -> str:
    if previous == current:
        return "upstream-unchanged"
    if Path(name).suffix.casefold() in {".py", ".ts", ".js", ".cjs", ".mjs", ".json", ".md", ".txt", ".html", ".css", ".cmd", ".pub", ""}:
        try:
            previous.decode("utf-8"); current.decode("utf-8")
            if b"\0" not in previous + current and previous.replace(b"\r\n", b"\n") == current.replace(b"\r\n", b"\n"):
                return "upstream-newlines-only"
        except UnicodeDecodeError:
            pass
    return "upstream-modified"


def privacy_findings(path: Path, relative: str) -> list[dict]:
    findings = []
    if private_path(relative):
        findings.append({"file": relative, "rule": "private-path"})
    if path.suffix.casefold() in {".py", ".ts", ".js", ".cjs", ".mjs", ".json", ".md", ".txt", ".cmd"}:
        data = path.read_bytes()
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append({"file": relative, "rule": name})
    return findings


def node_licenses(stage, source: Path, package: dict) -> list[dict]:
    rows = []
    for base, directories, files in os.walk(source, followlinks=False):
        excluded = stage.SKIP_PARTS - {"cache"} if package["name"] == "undici" else stage.SKIP_PARTS
        directories[:] = [name for name in directories if name.casefold() not in
                          excluded | {"node_modules", "docs", "benchmark", "benchmarks"} and
                          not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            relative = path.relative_to(source)
            if (is_license(path) and stage.safe_name(relative, node_package=package["name"]) and
                    stage.node_platform_file_matches(package["name"], relative) and not path.is_symlink()):
                rows.append(record(path, "node_modules/" + path.relative_to(stage.NODE_MODULES).as_posix()))
    # This package distributes its full MIT text in README rather than LICENSE.
    readme = source / "README.md"
    if package["name"] == "data-uri-to-buffer" and readme.is_file() and b"Permission is hereby granted" in readme.read_bytes():
        rows.append(record(readme, "node_modules/" + readme.relative_to(stage.NODE_MODULES).as_posix()))
    return sorted(rows, key=lambda item: item["file"])


def dependencies(stage) -> dict:
    python, omitted_python = stage.python_distributions()
    node, omitted_node = stage.node_distributions()
    py_rows, node_rows = [], []
    for dist in sorted(python, key=lambda d: d.metadata["Name"].casefold()):
        licenses = []
        for item in dist.files or []:
            path = Path(dist.locate_file(item)).resolve()
            if (stage.inside(path, stage.PYTHON_SITE) and path.is_file() and
                    is_license(path) and stage.safe_name(Path(item))):
                licenses.append(record(path, "runtime/python/Lib/site-packages/" +
                                       path.relative_to(stage.PYTHON_SITE).as_posix()))
        declaration = dist.metadata.get("License-Expression") or dist.metadata.get("License")
        licenses.extend(supplemental_licenses(dist.metadata["Name"], dist.version))
        classifiers = [c for c in dist.metadata.get_all("Classifier", []) if c.startswith("License ::")]
        py_rows.append({"name": dist.metadata["Name"], "version": dist.version,
                        "licenseDeclaration": declaration, "licenseClassifiers": classifiers,
                        "licenseFiles": sorted(licenses, key=lambda item: item["file"])})
    for source, package in sorted(node, key=lambda entry: (entry[1]["name"], str(entry[0]))):
        licenses = node_licenses(stage, source, package)
        licenses.extend(supplemental_licenses(package["name"], package["version"]))
        if package["name"] in {"onnxruntime-node", "onnxruntime-common"} and package["version"] == "1.30.0":
            licenses.append(record(ROOT / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"))
        if package["name"] == "@esbuild/win32-x64":
            parent = next(((p, d) for p, d in node if d["name"] == "esbuild" and d["version"] == package["version"]), None)
            if parent:
                licenses.extend(node_licenses(stage, *parent))
        node_rows.append({"name": package["name"], "version": package["version"],
                          "path": source.relative_to(stage.NODE_MODULES).as_posix(),
                          "licenseDeclaration": package.get("license"),
                          "repository": package.get("repository"),
                          "licenseFiles": licenses})
    return {"python": py_rows, "node": node_rows,
            "omittedPythonOptional": omitted_python, "omittedNodeOptional": omitted_node}


def supplemental_licenses(name: str, version: str, root: Path = ROOT) -> list[dict]:
    manifest = root / "licenses/supplemental-sources.json"
    rows = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    result = []
    for row in rows:
        if row["package"].casefold() != name.casefold() or row["version"] != version:
            continue
        relative = Path(row["file"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "licenses":
            raise ValueError("unsafe supplemental license path")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing or linked supplemental license")
        item = record(path, relative.as_posix())
        if item["sha256"] != row["sha256"]:
            raise ValueError("supplemental license hash differs")
        item["source"] = row["source"]
        result.append(item)
    return result


def origins(client, root: Path = ROOT) -> tuple[list, list]:
    names = list(client.PUBLIC_FILES)
    for directory, values in (("scripts", client.SCRIPTS), ("bridge", client.BRIDGE),
                              ("native-reader/wr", client.NATIVE_READER), ("electron/laya", client.LAYA)):
        names.extend(f"{directory}/{value}" for value in values)
    baseline_names = set(subprocess.check_output(["git", "ls-tree", "-r", "--name-only", BASELINE], cwd=root).decode().splitlines())
    rows, findings = [], []
    if len(names) != len(set(names)):
        raise ValueError("duplicate package allowlist input")
    for name in sorted(names):
        path = root / name
        if not path.is_file() or path.is_symlink() or any(
                parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
                for parent in [path, *path.parents] if parent.is_relative_to(root)):
            raise ValueError(f"missing or linked package input: {name}")
        row = record(path, name)
        if name in baseline_names:
            previous = subprocess.check_output(["git", "show", f"{BASELINE}:{name}"], cwd=root)
            before = hashlib.sha256(previous).hexdigest()
            row.update({"baselineSha256": before, "origin": source_origin(previous, path.read_bytes(), name)})
        else:
            row["origin"] = "added-after-baseline"
        rows.append(row)
        findings.extend(privacy_findings(path, name))
    return rows, findings


def audit() -> dict:
    stage = load("audit_runtime", "stage-real-runtime.py")
    client = load("audit_client", "stage-real-client.py")
    deps = dependencies(stage)
    inputs, findings = origins(client)
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    tracked_private = [name for name in tracked if name and private_path(name)]
    unresolved = []
    for ecosystem in ("python", "node"):
        for package in deps[ecosystem]:
            if not package["licenseFiles"]:
                unresolved.append({"ecosystem": ecosystem, "package": package["name"],
                                   "version": package["version"], "reason": "no separate staged license file"})
    node = shutil_node_version()
    runtime_licenses = []
    for path, relative in ((stage.PYTHON_ROOT / "LICENSE.txt", "runtime/python/LICENSE.txt"),
                           (ROOT / "licenses/node-LICENSE-24.11.1.txt", "runtime/node/LICENSE"),
                           (ROOT / "node_modules/electron/dist/LICENSE", "LICENSE.electron.txt"),
                           (ROOT / "node_modules/electron/dist/LICENSES.chromium.html", "LICENSES.chromium.html")):
        runtime_licenses.append(record(path, relative) if path.is_file() else {"file": relative, "missing": True})
    return {"schema": 1, "evidence": "source-inspected/installed-dependency-inventory",
            "releaseApproved": False, "baseline": BASELINE,
            "pythonVersion": sys.version.split()[0], "nodeVersion": node,
            "requiredBuildNode": "v24.11.1", "dependencies": deps, "publicInputs": inputs,
            "runtimeLicenses": runtime_licenses,
            "modelPins": json.loads((ROOT / "scripts/model-files.json").read_text(encoding="utf-8")),
            "privacy": {"publicInputFindings": findings, "trackedPrivatePaths": tracked_private,
                        "scope": "allowlisted text signatures and tracked path names only; not semantic proof"},
            "licenseReviewRequired": unresolved,
            "externalNotBundled": ["QQ", "qq-chat-exporter", "NapCatQQ"],
            "remainingGates": ["G1", "G2", "G3", "G4", "real-device", "user-acceptance"]}


def shutil_node_version() -> str:
    import shutil
    executable = os.environ.get("WECHATVIBE_BUILD_NODE") or shutil.which("node")
    if not executable:
        return "unavailable"
    return subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.absolute()
    if output.exists():
        raise ValueError("audit output must be a new directory")
    result = audit()
    output.mkdir(parents=True)
    (output / "delivery-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# QQVibe 分发依赖清单", "", "自动读取实际打包闭包；声明和随包许可文件分别记录。没有发布放行效力。", "",
             "| 生态 | 名称 | 版本 | 随包许可文件数 |", "|---|---|---|---|"]
    for ecosystem in ("python", "node"):
        for package in result["dependencies"][ecosystem]:
            lines.append(f"| {ecosystem} | `{package['name']}` | {package['version']} | {len(package['licenseFiles'])} |")
    lines += ["", "具体许可声明、文件路径、SHA-256和缺件见同目录 delivery-audit.json。",
              f"当前需复核许可项：{len(result['licenseReviewRequired'])}。",
              f"包前文本命中：{len(result['privacy']['publicInputFindings'])}；跟踪私有路径：{len(result['privacy']['trackedPrivatePaths'])}。"]
    (output / "DEPENDENCIES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"python": len(result['dependencies']['python']), "node": len(result['dependencies']['node']),
                      "publicInputs": len(result['publicInputs']), "licenseReviewRequired": len(result['licenseReviewRequired']),
                      "privacyFindings": len(result['privacy']['publicInputFindings']),
                      "trackedPrivatePaths": len(result['privacy']['trackedPrivatePaths']), "releaseApproved": False}))
    return 1 if result["privacy"]["publicInputFindings"] or result["privacy"]["trackedPrivatePaths"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

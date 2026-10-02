"""Build a portable preview from a fresh, allowlisted stage under .local.

The unique build directory is retained on failure for inspection. This command
does not sign, publish, or remove any previous build or user data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from product_profile import current_product
import importlib.util
_prereq_spec = importlib.util.spec_from_file_location("build_prerequisites", Path(__file__).with_name("build-prerequisites.py"))
_prereq = importlib.util.module_from_spec(_prereq_spec)
_prereq_spec.loader.exec_module(_prereq)
TEMPLATE = ROOT / "electron-builder.real-client.json"
PLACEHOLDERS = {
    "output": "__CLEAN_BUILD_OUTPUT__",
    "electronDist": "__ELECTRON_DIST__",
    "stage": "__CLEAN_STAGE_CLIENT__",
    "nodeModules": "__CLEAN_STAGE_NODE_MODULES__",
}
ASAR_CHECK = r"""
const asar = require('@electron/asar');
const crypto = require('node:crypto');
const archive = process.argv[1];
const names = JSON.parse(process.argv[2]);
const values = {};
for (const name of names) {
  const data = asar.extractFile(archive, name);
  values[name] = crypto.createHash('sha256').update(data).digest('hex');
}
const packageJson = JSON.parse(asar.extractFile(archive, 'package.json').toString('utf8'));
process.stdout.write(JSON.stringify({files: values, version: packageJson.version,
  name: packageJson.name, main: packageJson.main}));
"""


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def scan_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError(f"unsafe or missing package directory: {root}")
    found = {}
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError(f"link in package tree: {path}")
            if not path.is_dir() and not path.is_file():
                raise ValueError(f"unexpected package entry: {path}")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                folded = relative.casefold()
                if folded in found:
                    raise ValueError(f"case-colliding package file: {relative}")
                found[folded] = path
    return found


def manifest_files(build_dir: Path) -> tuple[dict[str, dict], dict]:
    runtime = json.loads((build_dir / "runtime-manifest.json").read_text(encoding="utf-8"))
    client = json.loads((build_dir / "client-files.json").read_text(encoding="utf-8"))
    rows = runtime["files"] + client["files"]
    expected = {}
    for row in rows:
        relative = row["file"]
        path = Path(relative)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise ValueError(f"unsafe manifest path: {relative}")
        folded = relative.casefold()
        if folded in expected:
            raise ValueError(f"duplicate manifest path: {relative}")
        expected[folded] = row
    return expected, client


def verify_tree(root: Path, expected: dict[str, dict]) -> None:
    found = scan_files(root)
    if set(found) != set(expected):
        extra = sorted(set(found) - set(expected))[:5]
        missing = sorted(set(expected) - set(found))[:5]
        raise ValueError(f"package inventory differs; unexpected={extra}, missing={missing}")
    for folded, row in expected.items():
        path = found[folded]
        if path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError(f"package hash differs: {row['file']}")
    expected_dirs = set()
    for name in expected:
        parts = Path(name).parts
        expected_dirs.update(Path(*parts[:index]).as_posix().casefold()
                             for index in range(1, len(parts)))
    actual_dirs = set()
    for base, directories, _ in os.walk(root, followlinks=False):
        for name in directories:
            actual_dirs.add(((Path(base) / name).relative_to(root)).as_posix().casefold())
    if actual_dirs != expected_dirs:
        raise ValueError("package directory inventory differs")


def generate_config(root: Path, build_dir: Path, electron_dist: Path) -> Path:
    if not (electron_dist / "electron.exe").is_file():
        raise ValueError(f"Electron dist lacks electron.exe: {electron_dist}")
    config = json.loads((root / "electron-builder.real-client.json").read_text(encoding="utf-8"))
    resources = config["extraResources"]
    if (config["directories"]["output"] != PLACEHOLDERS["output"] or
            config["electronDist"] != PLACEHOLDERS["electronDist"] or
            len(resources) != 2 or resources[0]["from"] != PLACEHOLDERS["stage"] or
            resources[1]["from"] != PLACEHOLDERS["nodeModules"]):
        raise ValueError("builder template changed; review clean-stage paths before building")
    stage = build_dir / "client"
    product = current_product()
    config["appId"] = product.app_id
    config["productName"] = product.product_name
    config["extraMetadata"]["name"] = product.product_name.lower()
    config["extraMetadata"]["description"] = product.product_name + " local chat analysis and portraits"
    config["win"]["executableName"] = product.product_name
    config["win"]["icon"] = product.icon_ico
    config["directories"]["output"] = str(build_dir / "release")
    config["electronDist"] = str(electron_dist)
    resources[0]["from"] = str(stage)
    resources[1]["from"] = str(stage / "node_modules")
    guide_files = config.get("extraFiles", [])
    if guide_files != [{"from": "__CLEAN_STAGE_GUIDE__", "to": "使用说明.html"}]:
        raise ValueError("offline manual template differs from reviewed path")
    guide_files[0]["from"] = str(stage / "docs/public/USER-GUIDE.html")
    output = build_dir / "electron-builder.generated.json"
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def verify_asar(root: Path, unpacked: Path, node_exe: Path, version: str,
                expected: dict[str, dict]) -> None:
    config = json.loads((root / "electron-builder.real-client.json").read_text(encoding="utf-8"))
    scripts = [name for name in config["files"] if name.startswith("scripts/")]
    if not scripts or any(name.casefold() not in expected for name in scripts):
        raise ValueError("app.asar script list differs from staged allowlist")
    archive = unpacked / "resources" / "app.asar"
    if not archive.is_file():
        raise ValueError("packaged app.asar missing")
    completed = subprocess.run(
        [str(node_exe), "-e", ASAR_CHECK, str(archive), json.dumps(scripts)],
        cwd=root, check=True, capture_output=True, text=True,
    )
    report = json.loads(completed.stdout)
    if (report["version"] != version or report["name"] != current_product().product_name.lower() or
            report["main"] != "scripts/desktop-main.cjs"):
        raise ValueError("app.asar source version or entry point differs")
    for name in scripts:
        if report["files"].get(name) != expected[name.casefold()]["sha256"]:
            raise ValueError(f"app.asar script hash differs: {name}")


def verify_package(root: Path, build_dir: Path, node_exe: Path) -> dict:
    expected, client = manifest_files(build_dir)
    verify_tree(build_dir / "client", expected)
    version = client["sourceVersion"]
    if digest(root / "package.json") != client["sourcePackageSha256"]:
        raise ValueError("source package.json changed during build")
    unpacked = build_dir / "release" / "win-unpacked"
    executable = unpacked / (current_product().product_name + ".exe")
    if not executable.is_file() or executable.stat().st_size == 0:
        raise ValueError("packaged product executable missing")
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("packaged product executable header differs")
    packaged_client = unpacked / "resources" / "client"
    verify_tree(packaged_client, expected)
    packaged_version = json.loads((packaged_client / "package.json").read_text(encoding="utf-8"))["version"]
    if packaged_version != version:
        raise ValueError("packaged client version differs from source")
    verify_asar(root, unpacked, node_exe, version, expected)
    if "docs/public/user-guide.html" in expected:
        guide = unpacked / "使用说明.html"
        if not guide.is_file() or digest(guide) != expected["docs/public/user-guide.html"]["sha256"]:
            raise ValueError("root offline user manual differs from staged manual")
    return {"buildDirectory": str(build_dir), "preview": str(unpacked),
            "version": version, "verifiedFiles": len(expected),
            "sourcePackageSha256": client["sourcePackageSha256"]}


def build(args: argparse.Namespace) -> dict:
    root = ROOT
    python_exe = Path(args.python_exe).resolve() if args.python_exe else Path(sys.executable).resolve()
    node_exe = _prereq.selected_node(args.node_exe, root)
    if not python_exe.is_file() or not node_exe.is_file():
        raise ValueError("Python or Node executable is missing")
    model_source = args.models_dir or os.environ.get("LAYA_MODEL_DIR") or root / ".models/laya"
    models = Path(model_source).resolve()
    electron_source = args.electron_dist or os.environ.get("WECHATVIBE_ELECTRON_DIST") or root / "node_modules/electron/dist"
    electron_dist = Path(electron_source).resolve()
    if not getattr(args, "without_model", False) and not models.is_dir():
        raise ValueError(f"Laya model directory is missing: {models}")
    if not (root / "node_modules/electron-builder/cli.js").is_file():
        raise ValueError("electron-builder is missing; run npm install first")
    build_root = current_product().state_dir("portable-builds", root)
    build_root.mkdir(parents=True, exist_ok=True)
    if build_root.is_symlink() or getattr(build_root, "is_junction", lambda: False)():
        raise ValueError("linked build root is forbidden")
    build_dir = Path(tempfile.mkdtemp(prefix="build-", dir=build_root))
    stage = build_dir / "client"
    environment = os.environ.copy()
    environment["WECHATVIBE_BUILD_NODE"] = str(node_exe)
    if args.node_license:
        environment["WECHATVIBE_NODE_LICENSE"] = str(Path(args.node_license).resolve())
    print(f"Clean build directory: {build_dir}", flush=True)
    subprocess.run([str(python_exe), str(root / "scripts/stage-real-runtime.py"),
                    "--output", str(stage)], cwd=root, env=environment, check=True)
    subprocess.run([str(python_exe), str(root / "scripts/stage-real-client.py"),
                    "--source-root", str(root), "--models-dir", str(models),
                    "--output", str(stage)] + (["--without-model"] if getattr(args, "without_model", False) else []) +
                   (["--source-commit", args.source_commit] if getattr(args, "source_commit", None) else []),
                   cwd=root, env=environment, check=True)
    expected, _ = manifest_files(build_dir)
    verify_tree(stage, expected)
    generated = generate_config(root, build_dir, electron_dist)
    subprocess.run([str(node_exe), str(root / "node_modules/electron-builder/cli.js"),
                    "--win", "--dir", "--config", str(generated)],
                   cwd=root, env=environment, check=True)
    result = verify_package(root, build_dir, node_exe)
    (build_dir / "build-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path, help="pinned external Laya model directory")
    parser.add_argument("--without-model", action="store_true", help="build a standard package with no Laya weights")
    parser.add_argument("--source-commit", help="freeze source inputs and include release provenance")
    parser.add_argument("--electron-dist", type=Path, help="directory containing electron.exe")
    parser.add_argument("--python-exe", type=Path, help="Python 3.14 executable with runtime dependencies")
    parser.add_argument("--node-exe", type=Path, help="Node 24.11.1 executable")
    parser.add_argument("--node-license", type=Path, help="Node runtime LICENSE file if absent beside node.exe")
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Clean portable build rejected: {error}\n")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

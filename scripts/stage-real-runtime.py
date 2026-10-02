"""Stage only the portable real-client runtimes and their recorded dependencies.

Run with the installed Python 3.14 interpreter. This script never copies a whole
site-packages or node_modules tree. Its output must be new and empty.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / ".local" / "real-client-package" / "client"
PYTHON_SOURCE = os.environ.get("WECHATVIBE_BUILD_PYTHON")
PYTHON_ROOT = (Path(PYTHON_SOURCE).resolve().parent if PYTHON_SOURCE
               else Path(sys.base_prefix).resolve())
PYTHON_SITE = Path(sysconfig.get_path("purelib")).resolve()
NODE_MODULES = ROOT / "node_modules"
PYTHON_ROOT_PACKAGES = (
    "wechatauto-replica", "cryptography", "zstandard", "psutil", "jieba",
    "Pillow", "uiautomation", "pywin32", "pyperclip", "colorama",
    "opencv-python", "numpy", "comtypes", "setuptools", "tzdata", "packaging",
)
# These are confined to optional upstream GUI/OCR/media paths, not this bridge.
# Never include them merely because another development tool installed them.
KNOWN_MISSING_OPTIONAL = {"winsdk", "imageio-ffmpeg", "pyautogui"}
NODE_ROOT_PACKAGES = (
    "@anthropic-ai/sdk", "@google/genai", "@huggingface/tokenizers",
    "ollama", "onnxruntime-node", "openai", "tsx", "undici",
)
SKIP_PARTS = {"__pycache__", "test", "tests", "testing", "demo", "demos", "examples",
              ".git", ".cache", "cache"}
SKIP_NAMES = {"direct_url.json", "auth.json", "credentials.json", ".gitkeep"}
STATS = {"python_stdlib": [0, 0], "python_packages": [0, 0],
         "node_runtime": [0, 0], "node_packages": [0, 0]}


def inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(source: Path, destination: Path, category: str) -> None:
    if source.is_symlink() or not source.is_file() or not inside(destination, STAGE):
        raise RuntimeError(f"unsafe stage source or target: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size != source.stat().st_size or sha256(destination) != sha256(source):
            raise RuntimeError(f"staged file differs; refusing overwrite: {destination.relative_to(STAGE)}")
        return
    shutil.copy2(source, destination)
    STATS[category][0] += 1
    STATS[category][1] += source.stat().st_size


def safe_name(path: Path, *, node_package: str | None = None) -> bool:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    excluded = SKIP_PARTS - {"cache"} if node_package == "undici" else SKIP_PARTS
    # SDKs ship importable credentials.mjs/js modules. These contain code, not
    # user credentials; the private JSON/key formats remain excluded below.
    sdk_credentials_code = (node_package in {"openai", "@anthropic-ai/sdk"} and
                            path.suffix.lower() in {".js", ".mjs", ".ts", ".mts"})
    return (not parts.intersection(excluded) and name not in SKIP_NAMES and
            (not name.startswith("credentials") or sdk_credentials_code) and
            path.suffix.lower() not in {".pyc", ".pyo", ".pdb", ".key", ".p12", ".pfx", ".pem"})


def stage_python_stdlib() -> dict:
    if (sys.version_info[:2] != (3, 14) or "t" in getattr(sys, "abiflags", "") or
            not (PYTHON_ROOT / "python.exe").is_file() or
            not PYTHON_SITE.is_dir()):
        raise RuntimeError("run with the intended base Python 3.14 or its venv")
    target = STAGE / "runtime" / "python"
    required = ("python.exe", "pythonw.exe", "python314.dll", "python3.dll",
                "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")
    for name in required:
        source = PYTHON_ROOT / name
        if not source.is_file():
            raise RuntimeError(f"Python runtime file missing: {name}")
        copy_file(source, target / name, "python_stdlib")
    unused_dlls = {"_tkinter.pyd", "_remote_debugging.pyd", "winsound.pyd",
                   "tcl86t.dll", "tk86t.dll"}
    for source in (PYTHON_ROOT / "DLLs").iterdir():
        if (source.is_file() and source.suffix.lower() in {".pyd", ".dll"} and
                "_d." not in source.name.lower() and "cp314t" not in source.name.lower() and
                "_test" not in source.name.lower() and
                source.name.lower() not in unused_dlls):
            copy_file(source, target / "DLLs" / source.name, "python_stdlib")
    library = PYTHON_ROOT / "Lib"
    excluded = SKIP_PARTS | {"site-packages", "ensurepip", "idlelib", "turtledemo",
                             "venv", "__phello__", "_pyrepl", "curses", "pydoc_data",
                             "tkinter"}
    for base, directories, files in os.walk(library):
        directories[:] = [name for name in directories if name.lower() not in excluded and
                         not (Path(base) / name).is_symlink()]
        for name in files:
            source = Path(base) / name
            relative = source.relative_to(library)
            if safe_name(relative):
                copy_file(source, target / "Lib" / relative, "python_stdlib")
    pth = target / "python314._pth"
    contents = ".\nDLLs\nLib\nLib\\site-packages\nimport site\n"
    if pth.exists() and pth.read_text(encoding="utf-8") != contents:
        raise RuntimeError("portable Python path file differs")
    if not pth.exists():
        pth.write_text(contents, encoding="utf-8")
    return {"version": sys.version.split()[0], "interpreter": "runtime/python/python.exe"}


def python_distributions() -> tuple[list, list]:
    pending = list(PYTHON_ROOT_PACKAGES)
    chosen = {}
    omitted = set()
    while pending:
        requested = pending.pop(0)
        canonical = requested.lower().replace("_", "-")
        if canonical in chosen or canonical in omitted:
            continue
        if canonical in KNOWN_MISSING_OPTIONAL:
            omitted.add(canonical)
            continue
        try:
            dist = metadata.distribution(requested)
        except metadata.PackageNotFoundError:
            if canonical in KNOWN_MISSING_OPTIONAL:
                omitted.add(canonical)
                continue
            raise RuntimeError(f"required installed Python distribution missing: {requested}") from None
        if not dist.files:
            raise RuntimeError(f"distribution has no RECORD: {requested}")
        chosen[canonical] = dist
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append(requirement.name)
    return list(chosen.values()), sorted(omitted)


def python_record_file_matches(relative: Path) -> bool:
    # QQVibe uses no video IO. Do not redistribute OpenCV's optional LGPL
    # FFmpeg DLL merely because it is present in the upstream wheel.
    return not (relative.parts and relative.parts[0].casefold() == "cv2" and
                relative.name.casefold().startswith("opencv_videoio_ffmpeg") and
                relative.suffix.casefold() == ".dll")


def stage_python_packages() -> dict:
    target = STAGE / "runtime" / "python" / "Lib" / "site-packages"
    chosen, omitted = python_distributions()
    summaries = []
    for dist in sorted(chosen, key=lambda item: item.metadata["Name"].lower()):
        count = size = 0
        for record in dist.files or []:
            source = Path(dist.locate_file(record)).resolve()
            if not inside(source, PYTHON_SITE):
                # Build/post-install scripts outside site-packages are not runtime imports.
                continue
            relative = source.relative_to(PYTHON_SITE.resolve())
            if not safe_name(relative) or not python_record_file_matches(relative) or not source.is_file():
                continue
            copy_file(source, target / relative, "python_packages")
            count += 1
            size += source.stat().st_size
        if count == 0:
            raise RuntimeError(f"distribution has no files in active site-packages: {dist.metadata['Name']}")
        summaries.append({"name": dist.metadata["Name"], "version": dist.version,
                          "recordedFiles": count, "recordedBytes": size})
    # This upstream .pth is in pywin32's RECORD and contains only relative imports.
    if not (target / "pywin32.pth").is_file():
        raise RuntimeError("pywin32.pth missing from staged RECORD")
    return {"packages": summaries, "declaredButAbsentOptional": omitted}


def node_package_dir(name: str, parent: Path | None = None) -> Path | None:
    parts = name.split("/")
    if name.startswith("@") and len(parts) != 2:
        raise RuntimeError("invalid scoped Node package")
    places = []
    if parent is not None:
        cursor = parent
        while inside(cursor, NODE_MODULES):
            places.append(cursor / "node_modules" / Path(*parts))
            if cursor == NODE_MODULES:
                break
            cursor = cursor.parent
    places.append(NODE_MODULES / Path(*parts))
    return next((place for place in places if (place / "package.json").is_file()), None)


def node_platform_matches(package: dict) -> bool:
    for field, current in (("os", "win32"), ("cpu", "x64")):
        values = package.get(field) or []
        if isinstance(values, str):
            values = [values]
        if f"!{current}" in values or (any(not item.startswith("!") for item in values) and
                                        current not in values):
            return False
    return True


def node_platform_file_matches(package_name: str, relative: Path) -> bool:
    parts = relative.parts
    if package_name == "onnxruntime-node" and parts[:2] == ("bin", "napi-v6"):
        return parts[2:4] == ("win32", "x64")
    return True


def node_distributions() -> tuple[list, list]:
    """Read-only installed runtime closure, shared by staging and license audits."""
    pending = [(name, None, False) for name in NODE_ROOT_PACKAGES]
    chosen = {}
    omitted = []
    while pending:
        name, parent, optional = pending.pop(0)
        if optional and ((name.startswith("@esbuild/") and name != "@esbuild/win32-x64") or
                         name == "fsevents"):
            continue
        source = node_package_dir(name, parent)
        if source is None:
            if optional:
                omitted.append(name)
                continue
            raise RuntimeError(f"Node package missing: {name}")
        resolved = source.resolve()
        if resolved in chosen:
            continue
        if not inside(resolved, NODE_MODULES):
            raise RuntimeError("Node package escapes installed modules")
        package = json.loads((source / "package.json").read_text(encoding="utf-8"))
        if optional and not node_platform_matches(package):
            omitted.append(name)
            continue
        chosen[resolved] = (source, package)
        pending.extend((dependency, source, False)
                       for dependency in (package.get("dependencies") or {}))
        pending.extend((dependency, source, True)
                       for dependency in (package.get("optionalDependencies") or {}))
    return list(chosen.values()), sorted(set(omitted))


def stage_node_packages() -> dict:
    chosen, omitted = node_distributions()
    summaries = []
    for source, package in sorted(chosen, key=lambda item: item[1]["name"]):
        relative_root = source.relative_to(NODE_MODULES)
        count = size = 0
        for base, directories, files in os.walk(source):
            excluded = SKIP_PARTS - {"cache"} if package["name"] == "undici" else SKIP_PARTS
            directories[:] = [name for name in directories if name.lower() not in excluded |
                             {"node_modules", ".git", "docs", "benchmark", "benchmarks"} and
                             not (Path(base) / name).is_symlink()]
            for name in files:
                current = Path(base) / name
                relative = current.relative_to(source)
                if (not safe_name(relative, node_package=package["name"]) or name.endswith(".map") or
                        not node_platform_file_matches(package["name"], relative)):
                    continue
                copy_file(current, STAGE / "node_modules" / relative_root / relative, "node_packages")
                count += 1
                size += current.stat().st_size
        summaries.append({"name": package["name"], "version": package["version"],
                          "path": str(relative_root).replace("\\", "/"),
                          "files": count, "bytes": size})
    return {"packages": summaries, "uninstalledOptional": sorted(set(omitted))}


def verify_staged_sdk_imports() -> None:
    """Reject a portable build whose filtered SDK tree cannot load at all."""
    node_exe = STAGE / "runtime" / "node" / "node.exe"
    for package in ("openai", "@anthropic-ai/sdk", "@google/genai", "ollama"):
        probe = subprocess.run(
            [str(node_exe), "--input-type=module", "-e", f"await import({json.dumps(package)})"],
            cwd=STAGE, capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError(f"staged model SDK cannot load: {package}")


def stage_node_runtime() -> dict:
    executable = os.environ.get("WECHATVIBE_BUILD_NODE") or shutil.which("node")
    if not executable:
        raise RuntimeError("node.exe not found; set WECHATVIBE_BUILD_NODE")
    source = Path(executable).resolve()
    if not source.is_file() or source.name.lower() != "node.exe":
        raise RuntimeError(f"invalid node.exe: {source}")
    version = subprocess.check_output([str(source), "--version"], text=True).strip()
    if version != "v24.11.1":
        raise RuntimeError(f"unexpected Node version: {version}")
    target = STAGE / "runtime" / "node"
    copy_file(source, target / "node.exe", "node_runtime")
    candidates = [source.parent / "LICENSE", source.parent / "LICENSE.txt",
                  ROOT / "licenses" / f"node-LICENSE-{version.lstrip('v')}.txt",
                  ROOT / ".local" / "real-client-package" / f"node-LICENSE-{version.lstrip('v')}.txt"]
    override = os.environ.get("WECHATVIBE_NODE_LICENSE")
    if override:
        candidates.insert(0, Path(override))
    license_source = next((candidate for candidate in candidates if candidate.is_file()), None)
    if license_source:
        copy_file(license_source, target / "LICENSE", "node_runtime")
    return {"version": version, "executable": "runtime/node/node.exe",
            "license": "runtime/node/LICENSE" if license_source else "MISSING"}


def staged_inventory(path: Path) -> dict:
    files = [item for item in path.rglob("*") if item.is_file()]
    return {"files": len(files), "bytes": sum(item.stat().st_size for item in files)}


def staged_hashes(path: Path) -> list[dict]:
    rows = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise RuntimeError(f"link in staged runtime: {item}")
        if item.is_file():
            rows.append({"file": item.relative_to(path).as_posix(),
                         "bytes": item.stat().st_size, "sha256": sha256(item)})
        elif not item.is_dir():
            raise RuntimeError(f"unexpected staged runtime entry: {item}")
    return rows


def main(argv: list[str] | None = None) -> int:
    global STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=STAGE,
                        help="new empty client staging directory")
    args = parser.parse_args(argv)
    STAGE = args.output.absolute()
    if STAGE.exists() and (not STAGE.is_dir() or STAGE.is_symlink() or
                           getattr(STAGE, "is_junction", lambda: False)() or
                           any(STAGE.iterdir())):
        raise RuntimeError(f"runtime stage must be new and empty: {STAGE}")
    STAGE.mkdir(parents=True, exist_ok=True)
    python = stage_python_stdlib()
    print(f"Python standard library staged: {STATS['python_stdlib'][0]} files")
    python.update(stage_python_packages())
    print(f"Python RECORD packages staged: {len(python['packages'])} distributions")
    node = stage_node_runtime()
    print(f"Node executable staged: {node['version']}; license={node['license']}")
    node.update(stage_node_packages())
    print(f"Node package closure staged: {len(node['packages'])} packages")
    verify_staged_sdk_imports()
    print("Staged model SDK imports verified")
    manifest = {"python": python, "node": node,
                "stagedInventory": {
                    "python": staged_inventory(STAGE / "runtime" / "python"),
                    "node": staged_inventory(STAGE / "runtime" / "node"),
                    "nodePackages": staged_inventory(STAGE / "node_modules"),
                },
                "copiedThisRun": {name: {"files": count, "bytes": size}
                                  for name, (count, size) in STATS.items()},
                "files": staged_hashes(STAGE)}
    package_root = STAGE.parent
    requirements = package_root / "python-requirements.lock.txt"
    requirements.write_text("".join(f"{item['name']}=={item['version']}\n"
                                    for item in python["packages"]), encoding="utf-8")
    node_lock = package_root / "node-dependencies.lock.json"
    node_lock.write_text(json.dumps(node["packages"], ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    output = package_root / "runtime-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

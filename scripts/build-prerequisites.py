"""Prepare a verified, already-installed Node runtime without changing PATH.

No download/install/publish/build. Only the official v24.11.1 win-x64 binary
matching SHASUMS256 may be selected; unequal existing output is preserved.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "v24.11.1"
NODE_SHA256 = "f13ac3ca23248dc389507e8fe38c34489ab7edb3e6d6700eb6da6a0b7e128eaf"
LOCAL_NODE = Path(".local/build-runtime/node-24.11.1/node.exe")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): result.update(block)
    return result.hexdigest()


def verify_node(path):
    path = Path(path).absolute()
    if not path.is_file() or path.is_symlink() or any(getattr(parent, "is_junction", lambda: False)() for parent in path.parents):
        raise ValueError("build Node file is missing or linked")
    if digest(path) != NODE_SHA256:
        raise ValueError("build Node differs from official 24.11.1 win-x64 hash")
    # Run only after the official binary identity was checked.
    if subprocess.check_output([str(path), "--version"], text=True, timeout=10).strip() != NODE_VERSION:
        raise ValueError("verified Node version differs")
    return path


def selected_node(explicit=None, root=None):
    root = Path(root or ROOT)
    configured = explicit or os.environ.get("WECHATVIBE_BUILD_NODE")
    if configured:
        return verify_node(configured)
    local = root / LOCAL_NODE
    if local.exists():
        return verify_node(local)
    candidate = shutil.which("node")
    if candidate:
        return verify_node(candidate)
    raise ValueError("build Node unavailable; prepare the existing pinned runtime or use --node-exe")


def prepare_node(source, license_file, root=None):
    root = Path(root or ROOT).absolute()
    source = verify_node(source)
    license_file = Path(license_file).absolute()
    license_bytes = license_file.read_bytes()
    bundled_license = (root / "licenses/node-LICENSE-24.11.1.txt").read_bytes()
    if license_bytes.replace(b"\r\n", b"\n") != bundled_license.replace(b"\r\n", b"\n"):
        raise ValueError("existing Node license differs from pinned source license")
    target = root / LOCAL_NODE
    for parent in target.parents:
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("linked build runtime directory")
        if parent == root: break
    target.parent.mkdir(parents=True, exist_ok=True)
    for original, destination in ((source, target), (license_file, target.parent / "LICENSE")):
        if destination.exists():
            if not destination.is_file() or destination.is_symlink() or digest(destination) != digest(original):
                raise ValueError("existing build runtime differs; refusing overwrite")
            continue
        temporary = destination.with_name(destination.name + ".part-" + uuid.uuid4().hex)
        try:
            shutil.copyfile(original, temporary)
            if digest(temporary) != digest(original): raise ValueError("runtime copy hash differs")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    verify_node(target)
    result = {"version": NODE_VERSION, "sha256": NODE_SHA256, "node": LOCAL_NODE.as_posix(),
              "licenseSha256": digest(target.parent / "LICENSE"), "pathChanged": False,
              "source": "existing-installed-runtime", "officialChecksums": "https://nodejs.org/dist/v24.11.1/SHASUMS256.txt"}
    (target.parent / "preparation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-node", type=Path, default=ROOT.parent / "WechatVibe/win-unpacked/resources/client/runtime/node/node.exe")
    parser.add_argument("--source-license", type=Path)
    args = parser.parse_args()
    result = prepare_node(args.source_node, args.source_license or args.source_node.parent / "LICENSE")
    print(json.dumps(result))


if __name__ == "__main__": main()

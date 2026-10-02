"""Create the pinned Laya model ZIP distributed beside the Windows client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAME = "WechatVibe-Laya-model-v1.zip"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def build(models: Path, output: Path) -> Path:
    pins = json.loads((ROOT / "scripts" / "model-files.json").read_text(encoding="utf-8"))
    if pins.get("schema") != 1 or not isinstance(pins.get("files"), dict):
        raise ValueError("model pin manifest is invalid")
    models = models.resolve(strict=True)
    entries = []
    for relative, expected in sorted(pins["files"].items()):
        source = models / relative
        if (not source.is_file() or source.is_symlink() or
                source.stat().st_size != expected["bytes"] or digest(source) != expected["sha256"]):
            raise ValueError("pinned model file differs: " + relative)
        entries.append((relative, source))
    output.mkdir(parents=True, exist_ok=True)
    target = output / NAME
    if target.exists():
        raise FileExistsError(target)
    handle, temporary = tempfile.mkstemp(prefix=".laya-model-", suffix=".tmp", dir=output)
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            for relative, source in entries:
                info = zipfile.ZipInfo("laya/" + relative, (1980, 1, 1, 0, 0, 0))
                info.create_system = 0
                info.external_attr = 0x20
                info.compress_type = zipfile.ZIP_DEFLATED
                with source.open("rb") as input_file, archive.open(info, "w", force_zip64=True) as result:
                    while chunk := input_file.read(1024 * 1024):
                        result.write(chunk)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != {
                    "laya/" + relative for relative, _ in entries}:
                raise ValueError("model archive verification failed")
        os.link(temporary, target)
        return target
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    asset = build(args.models_dir, args.output_dir)
    print(json.dumps({"name": asset.name, "bytes": asset.stat().st_size,
                      "sha256": digest(asset)}, ensure_ascii=True))


if __name__ == "__main__":
    main()

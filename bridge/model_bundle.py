"""Pinned Laya bundle validation shared by model selection and installation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "model-files.json"


class ModelBundleError(ValueError):
    pass


def pinned_files() -> dict[str, dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = data.get("files")
    if data.get("schema") != 1 or not isinstance(files, dict) or not files:
        raise ModelBundleError("模型文件清单无效")
    return files


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_model_dir(directory: Path, *, hashes: bool = True) -> Path:
    directory = Path(directory).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ModelBundleError("模型目录不存在")
    for relative, expected in pinned_files().items():
        path = directory / relative
        if not path.is_file() or path.stat().st_size != expected["bytes"]:
            raise ModelBundleError("模型文件不完整或版本不匹配")
        if hashes and checksum(path) != expected["sha256"]:
            raise ModelBundleError("模型文件校验失败")
    return directory


def available_model_dir(directory: Path) -> bool:
    try:
        validate_model_dir(directory, hashes=False)
        return True
    except (OSError, ModelBundleError):
        return False

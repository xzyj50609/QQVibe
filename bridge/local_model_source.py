"""Persist a user-selected local Laya directory across desktop updates."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from model_bundle import ModelBundleError, available_model_dir, validate_model_dir
from product_profile import current_product

PRODUCT = current_product()

_MISSING = object()


class ModelSource:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.downloaded = PRODUCT.state_dir("models", self.root) / "laya"
        self.bundled = self.root / ".models" / "laya"
        runtime = PRODUCT.state_dir("real-client-runtime", self.root)
        self.config = runtime / "local-model-source.json"
        self.legacy_config = runtime / "model-source.json"

    def _check_config(self, path: Path) -> None:
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            raise ModelBundleError("模型配置不可用")
        cursor = path
        while cursor != self.root:
            try:
                stat = cursor.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ModelBundleError("模型配置不可用") from exc
            else:
                if cursor.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    raise ModelBundleError("模型配置不可用")
            cursor = cursor.parent

    def _read_config(self, path: Path):
        self._check_config(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _MISSING

    def _selected_path(self):
        setting = self._read_config(self.config)
        if setting is _MISSING:
            setting = self._read_config(self.legacy_config)
            # The old shared filename also held encrypted API settings.
            if isinstance(setting, dict) and setting.get("version") == 1 and "schema" not in setting:
                return None
        return setting.get("path") if isinstance(setting, dict) and setting.get("schema") == 1 else None

    def status(self) -> dict:
        try:
            selected = self._selected_path()
        except (OSError, ValueError, ModelBundleError):
            selected = None
        if isinstance(selected, str) and selected and Path(selected).is_absolute():
            path = Path(selected)
            if path == self.bundled and not available_model_dir(path) and available_model_dir(self.downloaded):
                return {"state": "ready", "source": "downloaded", "path": str(self.downloaded)}
            source = "downloaded" if path == self.downloaded else "custom"
            return {"state": "ready" if available_model_dir(path) else "missing",
                    "source": source, "path": str(path)}
        for source, path in (("downloaded", self.downloaded), ("bundled", self.bundled)):
            if available_model_dir(path):
                return {"state": "ready", "source": source, "path": str(path)}
        return {"state": "missing", "source": "none", "path": str(self.downloaded)}

    def select(self, value: str) -> dict:
        if value == "downloaded":
            path = self.downloaded
        elif isinstance(value, str) and value and Path(value).is_absolute():
            path = Path(value)
        else:
            raise ModelBundleError("请选择有效的模型目录")
        try:
            path = validate_model_dir(path)
        except OSError as exc:
            raise ModelBundleError("模型目录不可用") from exc
        self._check_config(self.config)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self._check_config(self.config)
        temporary = self.config.with_name(self.config.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"schema": 1, "path": str(path)}, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config)
        finally:
            temporary.unlink(missing_ok=True)
        return self.status()

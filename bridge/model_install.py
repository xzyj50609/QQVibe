"""Install the pinned Release model ZIP into update-preserved local storage."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
import zipfile
from pathlib import Path

# The bundled Windows Python uses an isolated ._pth and does not add the
# invoked script's directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_bundle import ModelBundleError, available_model_dir, pinned_files, validate_model_dir
from product_profile import current_product

PRODUCT = current_product()


def plain_directory(path: Path) -> None:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ModelBundleError("模型存储路径不安全")
    if path.exists() and not path.is_dir():
        raise ModelBundleError("模型存储路径不是目录")
    if not path.exists():
        path.mkdir()


def install(archive_path: Path, root: Path) -> Path:
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ModelBundleError("软件目录不安全")
    archive_path = Path(archive_path)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ModelBundleError("模型压缩包不存在")
    local = PRODUCT.data_root(root)
    downloads = local / "model-downloads"
    for directory in (local, downloads):
        plain_directory(directory)
    if archive_path.resolve().parent != downloads.resolve() or archive_path.stat().st_size > 800_000_000:
        raise ModelBundleError("模型压缩包位置或大小无效")
    models = local / "models"
    plain_directory(models)
    target = models / "laya"
    if target.exists() or target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        if available_model_dir(target):
            return validate_model_dir(target)
        raise ModelBundleError("已有模型目录不完整，请先处理后重试")
    pins = pinned_files()
    temporary = models / (".laya-" + uuid.uuid4().hex)
    plain_directory(temporary)
    written = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            rows = archive.infolist()
            if len(rows) != len(pins) or {row.filename for row in rows} != {
                    "laya/" + relative for relative in pins}:
                raise ModelBundleError("模型压缩包内容不匹配")
            for row in rows:
                if row.flag_bits & 1 or row.file_size != pins[row.filename[5:]]["bytes"]:
                    raise ModelBundleError("模型压缩包文件大小不匹配")
                relative = row.filename[5:]
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(row) as source, destination.open("xb") as output:
                    written.append(destination)
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > row.file_size:
                            raise ModelBundleError("模型压缩包解压超限")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != row.file_size or digest.hexdigest() != pins[relative]["sha256"]:
                    raise ModelBundleError("模型文件校验失败")
        os.rename(temporary, target)
        return target
    finally:
        if temporary.exists():
            for path in written:
                path.unlink(missing_ok=True)
            tokenizer = temporary / "tokenizer"
            if tokenizer.exists():
                tokenizer.rmdir()
            temporary.rmdir()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: model_install.py ARCHIVE CLIENT_ROOT")
    try:
        destination = install(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print("模型安装失败：" + str(error), file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps({"installed": True, "path": str(destination)}, ensure_ascii=True))


if __name__ == "__main__":
    main()

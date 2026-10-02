"""Stage reviewed public client inputs into a new runtime stage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from product_profile import current_product
SCRIPTS = (
    "start-real-client.py", "start-real-client.cmd", "desktop-main.cjs",
    "real-client-shell.cjs", "real-client-preload.cjs", "real-client-recovery.cjs",
    "real-client-update.cjs", "real-client-update-proxy.cjs", "real-client-model.cjs",
    "real-client-update-controller.cjs",
    "real-client-update-helper.cjs", "real-client-update-extract.py",
    "update-signing.pub", "model-files.json", "model-asset.json",
    "restore-qq-data.py",
    "product-identity.json", "product-identity.cjs",
)
BRIDGE = (
    "account_api.py", "account_store.py", "conversation_selection.py",
    "analysis_server.ts", "batch_engine.py",
    "batch_state.py", "cache_source.py", "chat_server.py", "history_browser.py",
    "instance_identity.py", "live_source.py", "model_source.py", "model_bundle.py",
    "local_model_source.py", "model_install.py", "profile_signals.py", "profile_state.py",
    "backend_contracts.py", "backend_service.py", "message_results.py", "message_contracts.py",
    "message_input.py", "portrait_contracts.py", "api_tasks.py", "node_analysis.py",
    "analysis_scheduler.py",
    "analysis_control.py",
    "result_store.py",
    "wechat_source.py", "real_backend.py", "real_http.py", "snapshot_cache.py", "wechat_bridge.py",
    "product_profile.py", "qq_identity.py", "qq_message_store.py", "qq_source.py", "qq_normalize.py",
    "qq_history_browser.py",
    "qq_message_display.py",
    "qq_support.py",
    "qq_ingest_audit.py",
    "qq_account_store.py", "qq_account_api.py",
    "qce_protocol.py",
    "qq_connector.py",
    "qq_compatibility.py",
    "qq_data_management.py",
    "qq_sync_config.py", "qq_sync.py",
    "qq_standard_connection.py",
    "qq_entities.py",
    "analysis_targets.py",
    "qq_group_analysis.py",
    "qq_roles.py",
    "qq_sync_work.py",
    "qq_import.py", "qq_import_reader.py",
    "qq_analysis_store.py",
    "qq_api_analysis_store.py",
    "windows_file_owners.py",
)
NATIVE_READER = (
    "__init__.py", "crypto.py", "database.py", "discovery.py", "errors.py",
    "fixture.py", "log.py", "png_encode.py", "protocol.py", "service.py",
    "snapshot.py", "wal.py", "window.py", "window_capture.py", "window_uia.py",
)
LAYA = (
    "agent.ts", "calibration.ts", "catalog.ts", "context.ts", "expression.ts",
    "forecast.ts", "general-intent.ts", "grounded-intent.ts", "index.ts", "LICENSE",
    "message-batch.ts", "NOTICE", "options.ts", "personality.ts", "prompt.ts",
    "pyjson.ts", "questions.ts", "runner.ts", "scoring.ts", "social-cues.ts",
    "social-intents.ts", "style.ts", "tokenizer.ts", "types.ts",
)
MODEL_FILES = (
    "model.onnx", "onnx_config.json", "rl_agent_config.json", "README.md",
    "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json",
)
# One manifest pins staging, local selection and the separately downloaded model.
_model_manifest = json.loads((ROOT / "scripts/model-files.json").read_text(encoding="utf-8"))
MODEL_PINS = {name: (entry["bytes"], entry["sha256"])
              for name, entry in _model_manifest["files"].items()}
if _model_manifest.get("schema") != 1 or set(MODEL_FILES) != set(MODEL_PINS):
    raise RuntimeError("pinned model manifest differs from stage allowlist")
PUBLIC_FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "licenses/jieba-0.42.1-LICENSE.txt",
    "licenses/standardwebhooks-1.1.1-LICENSE.txt", "licenses/supplemental-sources.json",
    "NOTICE", "chatui/index.html",
    "docs/public/SETUP.md", "docs/public/COMPATIBILITY.md", "docs/public/KNOWN-ISSUES.md",
    "docs/public/BACKUP-UPGRADE.md", "docs/public/FEEDBACK.md", "docs/public/RELEASE-NOTES.md",
    "docs/public/R9-TRIAL.md", "docs/public/MODEL-LICENSE.md", "docs/public/BUILD.md",
    "docs/public/USER-GUIDE.md", "docs/public/USER-GUIDE.html",
    "docs/public/images/overview.png", "docs/public/images/single-chat.png",
    "docs/public/images/group-chat.png", "docs/public/images/settings.png",
    "docs/public/images/first-run.png", "docs/public/images/settings-connection.png",
    "docs/public/images/group-labels.png",
    "docs/migration/upstream/WechatVibe-1.2.2-THIRD_PARTY_NOTICES.md",
    "licenses/laya-model-mlx-LICENSE", "licenses/laya-model-mlx-NOTICE",
    "licenses/laya-model-mlx-README.txt", "licenses/laya-model-original-README.txt",
    "licenses/laya-model-sources.json",
    "chatui/app.js", "chatui/qq-startup.js", "chatui/qq-import-ui.js", "chatui/qq-sync-ui.js", "chatui/qq-support-ui.js", "chatui/qq-provenance-ui.js", "chatui/message-labels.js", "chatui/message-insight-adapters.js", "chatui/view-state.js", "chatui/style.css", "chatui/kaomoji.js",
    "chatui/data/analysis-catalog.json", "chatui/assets/wechatvibe-icon.png",
    "chatui/assets/wechatvibe-icon.ico", "electron/analysis.ts",
    "chatui/assets/qqvibe-icon.png", "chatui/assets/qqvibe-icon.ico", "chatui/assets/qqvibe-icon.svg",
    "electron/model-connectors.ts", "electron/api-insights.ts",
    "electron/api-message-insights.ts", "electron/api-portrait.ts", "electron/api-analysis-json.ts",
    "electron/local-message-insights.ts",
    "shared/contracts.ts", "shared/message-input.ts", "shared/group-context.ts", "src/lib/labels.ts",
    "native-reader/THIRD_PARTY_NOTICES.md",
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def inventory(root: Path) -> dict[str, Path]:
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError(f"linked staging root: {root}")
    found = {}
    if not root.exists():
        return found
    if not root.is_dir():
        raise ValueError(f"staging root is not a directory: {root}")
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            item = Path(base) / name
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise ValueError(f"link in staging tree: {item}")
            if not item.is_dir() and not item.is_file():
                raise ValueError(f"unexpected staging entry: {item}")
            if item.is_file():
                relative = item.relative_to(root).as_posix()
                folded = relative.casefold()
                if folded in found:
                    raise ValueError(f"case-colliding staging file: {relative}")
                found[folded] = item
    return found


def verify_directories(root: Path, files: set[str]) -> None:
    expected = set()
    for filename in files:
        parts = Path(filename).parts
        expected.update(Path(*parts[:index]).as_posix().casefold()
                        for index in range(1, len(parts)))
    actual = set()
    for base, directories, _ in os.walk(root, followlinks=False):
        for name in directories:
            actual.add(((Path(base) / name).relative_to(root)).as_posix().casefold())
    if actual != expected:
        raise ValueError("stage has unexpected or missing directories")


def verify_runtime_stage(output: Path) -> set[str]:
    manifest = output.parent / "runtime-manifest.json"
    existing = inventory(output)
    if not manifest.exists():
        if existing:
            raise ValueError("nonempty stage lacks runtime manifest")
        if output.exists():
            verify_directories(output, set())
        return set()
    rows = json.loads(manifest.read_text(encoding="utf-8")).get("files")
    if not isinstance(rows, list):
        raise ValueError("runtime manifest lacks exact file inventory")
    expected = {}
    for row in rows:
        relative = row["file"]
        path = Path(relative)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise ValueError("unsafe runtime inventory path")
        folded = relative.casefold()
        if folded in expected or not (relative.startswith("runtime/") or relative.startswith("node_modules/")):
            raise ValueError(f"unexpected runtime inventory path: {relative}")
        expected[folded] = row
    if set(existing) != set(expected):
        raise ValueError("runtime stage has unexpected or missing content")
    verify_directories(output, set(expected))
    for folded, row in expected.items():
        path = existing[folded]
        if path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError(f"runtime stage hash differs: {row['file']}")
    return set(expected)


def public_mappings(source: Path, models: Path, *, with_model: bool = True) -> list[tuple[Path, Path, Path]]:
    project_files = [Path(name) for name in PUBLIC_FILES]
    project_files += [Path("scripts") / name for name in SCRIPTS]
    project_files += [Path("bridge") / name for name in BRIDGE]
    project_files += [Path("native-reader/wr") / name for name in NATIVE_READER]
    project_files += [Path("electron/laya") / name for name in LAYA]
    mappings = [(source / relative, relative, source) for relative in project_files]
    if with_model:
        mappings += [(models / Path(name), Path(".models/laya") / name, models)
                     for name in MODEL_FILES]
    return mappings


def stage_public(source: Path, models: Path, output: Path, *, with_model: bool = True,
                 source_commit: str | None = None) -> dict:
    source, models, output = source.resolve(), models.resolve(), output.absolute()
    if output == source or source.is_relative_to(output) or output == models or models.is_relative_to(output):
        raise ValueError("staging output must not replace a source directory")
    if (output.parent / "client-files.json").exists():
        raise ValueError("client stage already has a manifest; create a new build directory")
    runtime_files = verify_runtime_stage(output)
    mappings = public_mappings(source, models, with_model=with_model)
    names = [relative.as_posix().casefold() for _, relative, _ in mappings]
    if len(names) != len(set(names)) or set(names) & runtime_files:
        raise ValueError("duplicate or colliding client staging path")
    version = json.loads((source / "package.json").read_text(encoding="utf-8"))["version"]
    if not isinstance(version, str) or not version:
        raise ValueError("source package version is invalid")
    prepared = []
    snapshot = None
    if source_commit and (source / "PUBLIC_SOURCE_MANIFEST.json").is_file():
        snapshot = json.loads((source / "PUBLIC_SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
        if snapshot.get("sourceCommit") != source_commit:
            raise ValueError("snapshot provenance differs from requested frozen commit")
        snapshot = {row["file"]: row for row in snapshot["files"]}
    if source_commit:
        source_metadata = source / "package.json"
        if snapshot is not None:
            row = snapshot.get("package.json", {})
            if row.get("bytes") != source_metadata.stat().st_size or row.get("sha256") != digest(source_metadata):
                raise ValueError("source package.json differs from frozen snapshot")
        else:
            frozen = subprocess.check_output(["git", "show", f"{source_commit}:package.json"], cwd=source)
            if frozen.replace(b"\r\n", b"\n") != source_metadata.read_bytes().replace(b"\r\n", b"\n"):
                raise ValueError("source package.json differs from frozen commit")
    for original, relative, allowed_root in mappings:
        if (original.is_symlink() or getattr(original, "is_junction", lambda: False)() or
                not original.is_file() or not original.resolve().is_relative_to(allowed_root)):
            raise ValueError(f"missing or unsafe public input: {relative.as_posix()}")
        length, checksum = original.stat().st_size, digest(original)
        if source_commit and allowed_root == source:
            if snapshot is not None:
                row = snapshot.get(relative.as_posix(), {})
                if row.get("bytes") != length or row.get("sha256") != checksum:
                    raise ValueError(f"input differs from frozen snapshot: {relative.as_posix()}")
            else:
                frozen = subprocess.check_output(["git", "show", f"{source_commit}:{relative.as_posix()}"], cwd=source)
                # Git text normalization may differ from the development checkout.
                current = original.read_bytes()
                if frozen != current and frozen.replace(b"\r\n", b"\n") != current.replace(b"\r\n", b"\n"):
                    raise ValueError(f"input differs from frozen commit: {relative.as_posix()}")
        if relative.parts[:2] == (".models", "laya"):
            model_name = Path(*relative.parts[2:]).as_posix()
            if model_name in MODEL_PINS and (length, checksum) != MODEL_PINS[model_name]:
                raise ValueError(f"pinned model hash differs: {model_name}")
        prepared.append((original, relative, length, checksum))
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for original, relative, length, checksum in prepared:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        if target.stat().st_size != length or digest(target) != checksum or digest(original) != checksum:
            raise ValueError(f"client input changed while staging: {relative.as_posix()}")
        rows.append({"file": relative.as_posix(), "bytes": length, "sha256": checksum})
    metadata = output / "package.json"
    metadata.write_text(json.dumps({"name": current_product().product_name.lower() + "-runtime", "version": version,
                                    "private": True, "type": "module"}, indent=2) + "\n",
                        encoding="utf-8")
    rows.append({"file": "package.json", "bytes": metadata.stat().st_size,
                 "sha256": digest(metadata)})
    if source_commit:
        release = output / "release-manifest.json"
        release.write_text(json.dumps({"schema": 1, "product": "QQVibe", "version": version,
            "candidate": version + "-light-guide", "sourceCommit": source_commit,
            "variant": "full" if with_model else "standard",
            "modelIncluded": with_model, "dataDirectory": "resources/client/QQVibeData",
            "automaticUpdates": False, "releaseIntent": "friends-pre-release",
            "crossMachineAccepted": False, "realApiOllamaAccepted": False,
            "modelRedistribution": "Apache-2.0-with-attached-attribution" if with_model else "not-included"
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows.append({"file": "release-manifest.json", "bytes": release.stat().st_size,
                     "sha256": digest(release)})
    expected = runtime_files | {row["file"].casefold() for row in rows}
    if set(inventory(output)) != expected:
        raise ValueError("client stage has unexpected or missing content")
    verify_directories(output, expected)
    result = {"sourceVersion": version, "sourcePackageSha256": digest(source / "package.json"),
              "files": rows}
    (output.parent / "client-files.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--without-model", action="store_true", help="stage the standard API-ready client without weights")
    parser.add_argument("--source-commit", help="freeze reviewed project inputs and include release provenance")
    parser.add_argument("--output", type=Path, required=True,
                        help="new stage created by the clean build driver")
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    models = (args.models_dir or source / ".models/laya").resolve()
    try:
        result = stage_public(source, models, args.output, with_model=not args.without_model,
                              source_commit=args.source_commit)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Client stage rejected: {error}\n")
    print(json.dumps({"publicFiles": len(result["files"]),
                      "bytes": sum(row["bytes"] for row in result["files"]),
                      "sourceVersion": result["sourceVersion"],
                      "userDataCopied": False, "output": str(args.output.absolute())}, ensure_ascii=True))


if __name__ == "__main__":
    main()

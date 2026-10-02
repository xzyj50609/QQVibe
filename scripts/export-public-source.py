"""Export only explicitly reviewed blobs from one frozen Git commit, never its history."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)

def safe(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError("unsafe export path")
    if any(x.casefold() in {".git", ".local", ".venv", "qqvibedata", "node_modules", "logs", "screenshots", "reviews"} for x in p.parts):
        raise ValueError("private or environment directory in allowlist")
    return p

def export(commit, output):
    sha = git("rev-parse", "--verify", commit + "^{commit}").decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", sha): raise ValueError("invalid frozen commit")
    spec = json.loads(git("show", sha + ":scripts/public-source-allowlist.json"))
    if spec.get("schema") != 1 or spec.get("reviewStatus") != "reviewed-explicit-inventory":
        raise ValueError("allowlist requires inventory review")
    prepared, seen = [], set()
    for row in spec["files"]:
        source, target = row["source"], row["target"]
        safe(source); safe(target)
        if target.casefold() in seen: raise ValueError("duplicate export path")
        seen.add(target.casefold())
        entry = git("ls-tree", sha, "--", source).decode().strip()
        if not entry.startswith(("100644 blob ", "100755 blob ")): raise ValueError("untracked, linked or missing export input: " + source)
        data = git("show", sha + ":" + source)
        if re.search(rb"\b(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{70,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,})\b", data):
            raise ValueError("possible literal credential in: " + source)
        prepared.append((target, data, source))
    output = output.absolute()
    if output.exists(): raise ValueError("export output must be a fresh directory")
    output.mkdir(parents=True)
    records = []
    for name, data, source in prepared:
        p = output / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        records.append({"file": name, "source": source, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"schema": 1, "product": "QQVibe", "sourceCommit": sha,
                "upstreamBaseline": "02b770821a1cf46b8ecc46b4f874ac6f922bd586",
                "gitHistoryExported": False, "allowlistReviewed": True,
                "files": records}
    (output / "PUBLIC_SOURCE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return {"sourceCommit": sha, "exportedFiles": len(records), "output": str(output)}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", required=True); p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(); print(json.dumps(export(args.commit, args.output)))

if __name__ == "__main__": main()

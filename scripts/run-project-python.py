from __future__ import annotations
import subprocess
import sys
from pathlib import Path
from project_python import ROOT, select_python


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run-project-python.py <script> [args...]", file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    if not target.is_absolute():
        target = ROOT / target
    completed = subprocess.run([str(select_python()), str(target), *sys.argv[2:]], cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

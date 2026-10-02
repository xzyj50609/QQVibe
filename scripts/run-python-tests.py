from __future__ import annotations

import subprocess
import sys
from pathlib import Path


from project_python import ROOT, select_python


def use_project_python() -> int | None:
    python = select_python()
    if Path(sys.executable).resolve() == python.resolve():
        return None
    return subprocess.run([str(python), str(Path(__file__).resolve())], cwd=ROOT).returncode


def run_group(directory: str, pattern: str) -> None:
    test_root = ROOT / directory
    files = sorted(test_root.glob(pattern))
    if not files:
        raise RuntimeError(f"No tests found in {test_root}")
    for path in files:
        print(f"===== {path.relative_to(ROOT)} =====", flush=True)
        completed = subprocess.run([sys.executable, str(path)], cwd=path.parent)
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)


def main() -> int:
    project_result = use_project_python()
    if project_result is not None:
        return project_result
    run_group("bridge", "test_*.py")
    run_group("scripts", "test-*.py")
    run_group("native-reader", "test_*.py")
    run_group("probes", "test_*.py")
    print("ALL_PYTHON_TESTS_PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

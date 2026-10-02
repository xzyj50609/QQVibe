"""Select a project interpreter without overriding an explicitly active environment."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]

def select_python() -> Path:
    override = os.environ.get("WECHATVIBE_PYTHON")
    if override:
        candidate = Path(shutil.which(override) or override).resolve()
        if not candidate.is_file():
            raise FileNotFoundError("WECHATVIBE_PYTHON does not identify an installed interpreter")
        return candidate
    active = os.environ.get("VIRTUAL_ENV")
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    if active and (Path(active) / relative).is_file():
        return Path(active) / relative
    if sys.prefix != sys.base_prefix:
        return Path(sys.executable)
    project = ROOT / ".venv" / relative
    return project if project.is_file() else Path(sys.executable)

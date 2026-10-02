from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import project_python as selector


class ProjectPythonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechatvibe-python-selector-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        self.project = self.root / ".venv" / self.relative
        self.project.parent.mkdir(parents=True)
        self.project.touch()
        for context in (patch.object(selector, "ROOT", self.root), patch.dict(os.environ, {}, clear=True),
                        patch.object(sys, "prefix", "base"), patch.object(sys, "base_prefix", "base")):
            context.start(); self.addCleanup(context.stop)

    def test_project_fallback(self):
        self.assertEqual(selector.select_python(), self.project)

    def test_explicit_override(self):
        with patch.dict(os.environ, {"WECHATVIBE_PYTHON": sys.executable}):
            self.assertEqual(selector.select_python(), Path(sys.executable).resolve())

    def test_invalid_override_fails_closed(self):
        with patch.dict(os.environ, {"WECHATVIBE_PYTHON": str(self.root / "missing.exe")}):
            with self.assertRaises(FileNotFoundError): selector.select_python()

    def test_active_virtualenv_takes_precedence(self):
        active = self.root / "active" / self.relative
        active.parent.mkdir(parents=True); active.touch()
        with patch.dict(os.environ, {"VIRTUAL_ENV": str(self.root / "active")}):
            self.assertEqual(selector.select_python(), active)

    def test_running_venv_is_not_redirected(self):
        with patch.object(sys, "prefix", "active"):
            self.assertEqual(selector.select_python(), Path(sys.executable))

    def test_no_project_falls_back_to_current_python(self):
        self.project.unlink()
        self.assertEqual(selector.select_python(), Path(sys.executable))

if __name__ == "__main__": unittest.main()

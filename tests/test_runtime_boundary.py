"""The runtime dependency promise, enforced.

`requirements.txt` says "No third-party Python packages required." This test
reads every import in `app/` and `server.py` and fails if any of them resolves
outside the standard library. Development tooling in `requirements-dev.txt` and
`tests/` is deliberately out of scope.
"""
from __future__ import annotations

import ast
import importlib.util
import sysconfig
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = sorted(ROOT.glob("app/**/*.py")) + [ROOT / "server.py"]
LOCAL_ROOTS = {"app", "server", "tests"}
STDLIB_DIR = Path(sysconfig.get_paths()["stdlib"]).resolve()


def module_imports(source: str) -> set:
    """Top-level module names imported by `source`, ignoring relative imports."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def is_standard_library(name: str) -> bool:
    if name in LOCAL_ROOTS:
        return True
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    if spec.origin in (None, "built-in", "frozen"):
        return True
    origin = Path(spec.origin).resolve()
    if "site-packages" in origin.parts or "dist-packages" in origin.parts:
        return False
    return STDLIB_DIR in origin.parents or origin.parent == STDLIB_DIR


class RuntimeBoundaryTestCase(unittest.TestCase):
    def test_runtime_files_were_discovered(self):
        self.assertGreaterEqual(len(RUNTIME_FILES), 12, "the runtime file list is empty or truncated")
        self.assertIn(ROOT / "server.py", RUNTIME_FILES)

    def test_runtime_imports_are_standard_library_only(self):
        offenders = {}
        for path in RUNTIME_FILES:
            for name in sorted(module_imports(path.read_text(encoding="utf-8"))):
                if not is_standard_library(name):
                    offenders.setdefault(str(path.relative_to(ROOT)).replace("\\", "/"), []).append(name)
        self.assertEqual(offenders, {}, "requirements.txt promises a standard-library runtime; these imports break it: {0}".format(offenders))

    def test_detector_rejects_a_third_party_import(self):
        """The gate must be able to fail: a known third-party name is not stdlib."""
        self.assertFalse(is_standard_library("yaml"))
        self.assertFalse(is_standard_library("definitely_not_installed_package_xyz"))
        self.assertTrue(is_standard_library("sqlite3"))
        self.assertEqual(module_imports("import yaml\nfrom app.db import init_db\n"), {"yaml", "app"})

    def test_requirements_file_still_states_the_promise(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertIn("standard library only", text)


if __name__ == "__main__":
    unittest.main()

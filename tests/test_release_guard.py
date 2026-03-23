"""Release guard tests."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "release_guard.py"


def _load_release_guard():
    spec = importlib.util.spec_from_file_location("release_guard", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("release_guard", module)
    spec.loader.exec_module(module)
    return module


class ReleaseGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.guard = _load_release_guard()

    def test_main_passes_for_repo_versions(self) -> None:
        self.assertEqual(self.guard.main([]), 0)

    def test_main_accepts_matching_v_tag(self) -> None:
        version = self.guard.load_pyproject_version()
        self.assertEqual(self.guard.main([f"--tag=v{version}"]), 0)

    def test_main_rejects_mismatched_tag(self) -> None:
        with self.assertRaisesRegex(SystemExit, "tag/version mismatch"):
            self.guard.main(["--tag=v999.0.0"])

    def test_normalize_tag_handles_github_ref(self) -> None:
        self.assertEqual(self.guard.normalize_tag("refs/tags/v1.2.3"), "1.2.3")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "tools" / "check_git_boundary.py"


class GitBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        subprocess.run(
            ["git", "init", "--quiet", str(self.root)],
            check=True,
            capture_output=True,
        )
        (self.root / ".gitignore").write_text(
            "/vault/\n"
            "/trash/\n"
            "/imports/raw/\n"
            "/imports/work/\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_ignored_untracked_private_paths_pass(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Git data boundary passed", result.stdout)

    def test_tracked_private_file_fails_without_revealing_filename(self) -> None:
        path = self.root / "vault" / "private-secret-title.md"
        path.parent.mkdir()
        path.write_text("private", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--force", str(path)],
            check=True,
            capture_output=True,
        )

        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vault/ has 1 tracked private file", result.stdout)
        self.assertNotIn("private-secret-title", result.stdout)


if __name__ == "__main__":
    unittest.main()

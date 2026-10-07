from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "tools" / "check_history.py"
SYNTHETIC_SECRET = "sk-" + "A" * 24
GIT = shutil.which("git")


def git(root: Path, *arguments: str) -> None:
    if GIT is None:
        raise unittest.SkipTest("git is unavailable")
    result = subprocess.run(
        [GIT, "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)


class HistoryCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        git(self.root, "init", "--quiet")
        git(self.root, "config", "user.name", "Synthetic Tester")
        git(self.root, "config", "user.email", "synthetic@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root)],
            text=True,
            capture_output=True,
            check=False,
        )

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", message)

    def test_clean_history_passes(self) -> None:
        (self.root / "README.md").write_text("synthetic\n", encoding="utf-8")
        self.commit("Add synthetic readme")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deleted_secret_is_detected_without_echoing_its_value(self) -> None:
        path = self.root / "old.txt"
        path.write_text(f"token={SYNTHETIC_SECRET}\n", encoding="utf-8")
        self.commit("Add synthetic credential")
        path.unlink()
        self.commit("Delete synthetic credential")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("possible OpenAI-style API key", result.stdout)
        self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)
        self.assertNotIn("old.txt", result.stdout + result.stderr)

    def test_deleted_private_path_is_detected_without_echoing_the_filename(
        self,
    ) -> None:
        path = self.root / "vault" / "private-note.md"
        path.parent.mkdir()
        path.write_text("synthetic private note\n", encoding="utf-8")
        self.commit("Add synthetic private path")
        path.unlink()
        self.commit("Delete synthetic private path")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("private path under vault/", result.stdout)
        self.assertNotIn("private-note.md", result.stdout + result.stderr)

    def test_journal_under_an_earlier_filename_remains_detectable(self) -> None:
        path = self.root / "docs" / "open-source-roadmap.md"
        path.parent.mkdir()
        path.write_text(
            "# Open-source roadmap\n\nBaseline commit: synthetic baseline\n",
            encoding="utf-8",
        )
        self.commit("Add synthetic operational journal")
        path.rename(path.parent / "renamed.md")
        self.commit("Move synthetic journal")
        (path.parent / "renamed.md").unlink()
        self.commit("Remove synthetic journal")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("retired operational journal", result.stdout)
        self.assertNotIn("synthetic baseline", result.stdout + result.stderr)
        self.assertNotIn("open-source-roadmap.md", result.stdout + result.stderr)

    def test_commit_context_is_audited_without_echoing_private_values(self) -> None:
        (self.root / "README.md").write_text("synthetic\n", encoding="utf-8")
        session = "https://claude.ai/code/session_synthetic"
        self.commit(
            "Add synthetic readme\n\n"
            f"token={SYNTHETIC_SECRET}\n{session}\n"
            "production Vault validation: 3 active, synthetic fixture only"
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        for kind in (
            "possible OpenAI-style API key in commit message",
            "private chat session link",
            "production vault statistics",
        ):
            self.assertIn(kind, result.stdout)
        self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)
        self.assertNotIn(session, result.stdout + result.stderr)

    def test_private_history_reachable_only_from_a_pull_ref_is_audited(self) -> None:
        (self.root / "README.md").write_text("synthetic\n", encoding="utf-8")
        self.commit("Add synthetic readme")
        git(self.root, "checkout", "--quiet", "-b", "synthetic-private-branch")
        private = self.root / "private.txt"
        private.write_text(SYNTHETIC_SECRET, encoding="utf-8")
        self.commit("Add synthetic private branch data")
        git(self.root, "update-ref", "refs/pull/1/head", "HEAD")
        git(self.root, "checkout", "--quiet", "-")
        git(self.root, "branch", "-D", "synthetic-private-branch")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("possible OpenAI-style API key", result.stdout)
        self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

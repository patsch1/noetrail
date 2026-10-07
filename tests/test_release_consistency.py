from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "tools" / "check_release_consistency.py"


class ReleaseConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        (self.root / "src" / "noetrail").mkdir(parents=True)
        (self.root / "docs" / "releases").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text(
            "[project]\n"
            'version = "1.2.3rc1"\n'
            "[tool.noetrail]\n"
            "schema-version = 7\n",
            encoding="utf-8",
        )
        (self.root / "src" / "noetrail" / "version.py").write_text(
            'FALLBACK_VERSION = "1.2.3rc1"\n',
            encoding="utf-8",
        )
        (self.root / "src" / "noetrail" / "migrations.py").write_text(
            "CURRENT_SCHEMA_VERSION = 7\n",
            encoding="utf-8",
        )
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 1.2.3rc1 - Unreleased\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "releases" / "1.2.3rc1.md").write_text(
            "# Noetrail 1.2.3rc1\n\nThe current core schema is 7.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_check(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(self.root),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_matching_release_sources_pass(self) -> None:
        result = self.run_check("--tag", "v1.2.3rc1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_published_release_with_valid_date_passes(self) -> None:
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n\n## 1.2.3rc1 - 2024-02-29\n",
            encoding="utf-8",
        )
        result = self.run_check("--tag", "v1.2.3rc1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_release_status_or_calendar_date_fails(self) -> None:
        statuses = ("Published", "2023-02-29", "2026-13-01", "20260101", "2026-1-01")
        for status in statuses:
            with self.subTest(status=status):
                (self.root / "CHANGELOG.md").write_text(
                    f"# Changelog\n\n## 1.2.3rc1 - {status}\n", encoding="utf-8"
                )
                result = self.run_check()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("changelog", result.stdout)

    def test_missing_or_ambiguous_version_heading_fails(self) -> None:
        for text in (
            "## 1.2.2 - Unreleased\n",
            "Text mentions ## 1.2.3rc1 - Unreleased\n",
            "## 1.2.3rc1 - Unreleased\n## 1.2.3rc1 - 2024-02-29\n",
        ):
            with self.subTest(text=text):
                (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
                result = self.run_check()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("exactly one heading", result.stdout)

    def test_mismatched_source_version_and_tag_fail(self) -> None:
        (self.root / "src" / "noetrail" / "version.py").write_text(
            'FALLBACK_VERSION = "1.2.2"\n',
            encoding="utf-8",
        )
        result = self.run_check("--tag", "v1.2.3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("source fallback", result.stdout)
        self.assertIn("release tag", result.stdout)

    def test_schema_claim_must_match_code_and_configuration(self) -> None:
        (self.root / "src" / "noetrail" / "migrations.py").write_text(
            "CURRENT_SCHEMA_VERSION = 8\n",
            encoding="utf-8",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("schema version", result.stdout)
        self.assertIn("release notes", result.stdout)


if __name__ == "__main__":
    unittest.main()

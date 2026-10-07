from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"

from noetrail.frontmatter import parse_frontmatter  # noqa: E402
from noetrail.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402


class MarkdownImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        initialized = self.run_cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                "--builtins-root",
                str(BUILTINS_ROOT),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def write_raw(self, relative: str, content: str) -> Path:
        path = self.data / "imports" / "raw" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_dry_run_apply_validate_and_repeat_are_idempotent(self) -> None:
        self.write_raw("markdown-demo/first.md", "# First imported note\n\nHello.\n")
        self.write_raw("markdown-demo/second-note.md", "No heading, still useful.\n")

        preview = self.run_cli(
            "import",
            "markdown",
            "--source",
            "markdown-demo",
            "--tag",
            "migration",
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        preview_report = json.loads(preview.stdout)
        self.assertTrue(preview_report["dry_run"])
        self.assertEqual(preview_report["candidate_count"], 2)
        self.assertEqual(len(preview_report["created"]), 2)
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

        applied = self.run_cli(
            "import",
            "markdown",
            "--source",
            "markdown-demo",
            "--tag",
            "migration",
            "--apply",
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        applied_report = json.loads(applied.stdout)
        self.assertFalse(applied_report["dry_run"])
        self.assertEqual(len(applied_report["created"]), 2)

        entries = sorted((self.data / "vault").rglob("*.md"))
        self.assertEqual(len(entries), 2)
        by_title = {}
        for path in entries:
            metadata, body = parse_frontmatter(path)
            by_title[metadata["title"]] = (metadata, body)
            self.assertEqual(
                metadata["schema_version"], CURRENT_SCHEMA_VERSION
            )
            self.assertEqual(metadata["type"], "note")
            self.assertEqual(metadata["status"], "unreviewed")
            self.assertEqual(metadata["origin"], "import")
            self.assertEqual(metadata["tags"], ["migration"])
            self.assertEqual(metadata["source"]["system"], "markdown")
            self.assertRegex(metadata["source"]["content_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("First imported note", by_title)
        self.assertIn("second note", by_title)
        self.assertIn("# First imported note", by_title["First imported note"][1])

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

        repeated = self.run_cli(
            "import",
            "markdown",
            "--source",
            "markdown-demo",
            "--apply",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        repeated_report = json.loads(repeated.stdout)
        self.assertEqual(repeated_report["created"], [])
        self.assertEqual(len(repeated_report["skipped"]), 2)
        self.assertEqual(len(list((self.data / "vault").rglob("*.md"))), 2)

    def test_changed_raw_source_is_reported_without_overwriting_entry(self) -> None:
        source = self.write_raw("changed/source.md", "# Original\n\nOne.\n")
        imported = self.run_cli(
            "import", "markdown", "--source", "changed", "--apply"
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        entry = next((self.data / "vault").rglob("*.md"))
        before = entry.read_bytes()

        source.write_text("# Changed\n\nTwo.\n", encoding="utf-8")
        repeated = self.run_cli("import", "markdown", "--source", "changed")
        self.assertEqual(repeated.returncode, 1)
        report = json.loads(repeated.stdout)
        self.assertEqual(report["planned_count"], 0)
        self.assertIn("raw source changed", report["errors"][0]["reason"])
        self.assertEqual(entry.read_bytes(), before)

    def test_preflight_error_prevents_partial_import(self) -> None:
        self.write_raw("mixed/valid.md", "# Valid\n")
        invalid = self.data / "imports" / "raw" / "mixed" / "invalid.md"
        invalid.write_bytes(b"\xff\xfe")

        result = self.run_cli(
            "import", "markdown", "--source", "mixed", "--apply"
        )
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["planned_count"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

    def test_sources_outside_raw_and_symbolic_links_are_rejected(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        escaped = self.run_cli(
            "import", "markdown", "--source", str(outside)
        )
        self.assertNotEqual(escaped.returncode, 0)
        self.assertIn("below imports/raw", escaped.stderr)

        target = self.write_raw("links/target.md", "# Target\n")
        link = self.data / "imports" / "raw" / "links" / "linked.md"
        link.symlink_to(target)
        linked = self.run_cli("import", "markdown", "--source", "links")
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn("symbolic links", linked.stderr)

    def test_limit_is_deterministic_and_reported_as_truncated(self) -> None:
        self.write_raw("limited/b.md", "# B\n")
        self.write_raw("limited/a.md", "# A\n")
        result = self.run_cli(
            "import", "markdown", "--source", "limited", "--limit", "1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["total_candidate_count"], 2)
        self.assertTrue(report["truncated"])
        self.assertTrue(report["created"][0]["source"].endswith("a.md"))


if __name__ == "__main__":
    unittest.main()

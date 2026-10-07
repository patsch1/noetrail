"""Regression tests for entries whose frontmatter holds the wrong JSON type.

Frontmatter values are parsed with ``json.loads``, so any field can end up
holding a list, an object or a number no matter what the schema says -- a bad
merge, a hand edit, or a third-party tool is enough. Every command below used
to abort with a ``TypeError`` traceback for the whole vault when a single entry
was in that state:

* an unhashable ``type`` (a list or an object) was used as a set member and as
  a registry key, so ``search``, ``inventory``, ``review``, ``relations`` and
  ``validate`` all raised ``TypeError: unhashable type``;
* a non-list ``tags`` was iterated by ``search``;
* a non-list ``relations`` was iterated by ``relations`` and by the symmetric
  check in ``relate``;
* an unhashable relation ``target`` was used as a dictionary key by
  ``relations``.

``validate`` is what has to report these entries; the other commands have to
stay usable.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class MalformedFrontmatterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        (self.root / ".knowledge").mkdir()
        (self.root / ".knowledge" / "SPEC.md").write_text("test", encoding="utf-8")
        (self.root / ".knowledge" / "relation-types.yaml").write_text(
            "relations:\n"
            "  related_to:\n"
            "    symmetric: true\n"
            "  involves:\n"
            "    inverse: experienced_in\n",
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge" / "packs" / "knowledge-core",
            self.root / ".knowledge" / "packs" / "knowledge-core",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI_COMMAND, "--root", str(self.root), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def capture(self, title: str) -> tuple[Path, str]:
        result = self.run_cli(
            "capture",
            "--type",
            "thought",
            "--title",
            title,
            "--text",
            "Ein synthetischer Notiztext.",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        return self.root / payload["created"], payload["id"]

    def set_field(self, path: Path, key: str, value: object) -> None:
        """Overwrite one frontmatter field with a raw JSON value."""

        lines = path.read_text(encoding="utf-8").splitlines()
        end = lines.index("---", 1)
        rendered = f"{key}: {json.dumps(value, separators=(',', ':'))}"
        header = [
            rendered if line.split(":", 1)[0] == key else line
            for line in lines[1:end]
        ]
        if not any(line.startswith(f"{key}:") for line in header):
            header.append(rendered)
        path.write_text(
            "\n".join(["---", *header, "---", *lines[end + 1 :]]) + "\n",
            encoding="utf-8",
        )

    def assertNoTraceback(
        self,
        result: subprocess.CompletedProcess[str],
    ) -> None:
        self.assertNotIn("Traceback", result.stderr)

    def test_validate_reports_unhashable_type_instead_of_crashing(self) -> None:
        path, _ = self.capture("Kaputter Typ")
        for broken in ([], {"nested": True}):
            with self.subTest(broken=broken):
                self.set_field(path, "type", broken)
                validated = self.run_cli("validate")
                self.assertNoTraceback(validated)
                self.assertEqual(validated.returncode, 1)
                self.assertIn("invalid type", validated.stdout)

    def test_read_commands_survive_unhashable_type(self) -> None:
        broken_path, _ = self.capture("Kaputter Typ")
        _, healthy_id = self.capture("Heile Notiz")
        self.set_field(broken_path, "type", ["thought"])

        searched = self.run_cli("search", "synthetischer")
        self.assertNoTraceback(searched)
        self.assertEqual(searched.returncode, 0, searched.stderr)
        self.assertIn(healthy_id, searched.stdout)

        for arguments in (
            ("inventory",),
            ("review", "--limit", "5"),
            ("relations", healthy_id),
        ):
            with self.subTest(command=arguments):
                result = self.run_cli(*arguments)
                self.assertNoTraceback(result)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_search_survives_non_list_tags(self) -> None:
        broken_path, _ = self.capture("Kaputte Tags")
        _, healthy_id = self.capture("Heile Notiz")
        self.set_field(broken_path, "tags", 42)

        searched = self.run_cli("search", "synthetischer")
        self.assertNoTraceback(searched)
        self.assertEqual(searched.returncode, 0, searched.stderr)
        self.assertIn(healthy_id, searched.stdout)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 1)
        self.assertIn("tags must be an array of strings", validated.stdout)

    def test_relations_survives_non_list_relations(self) -> None:
        path, entry_id = self.capture("Kaputte Relationen")
        self.set_field(path, "relations", 42)

        related = self.run_cli("relations", entry_id)
        self.assertNoTraceback(related)
        self.assertEqual(related.returncode, 0, related.stderr)
        payload = json.loads(related.stdout)
        self.assertEqual(payload["outgoing"], [])

    def test_relations_reports_unhashable_relation_target(self) -> None:
        path, entry_id = self.capture("Unhashbares Ziel")
        self.set_field(
            path,
            "relations",
            [{"predicate": "related_to", "target": ["kn_nope"]}],
        )

        related = self.run_cli("relations", entry_id)
        self.assertNoTraceback(related)
        self.assertEqual(related.returncode, 0, related.stderr)
        payload = json.loads(related.stdout)
        self.assertEqual(len(payload["outgoing"]), 1)
        self.assertIsNone(payload["outgoing"][0]["target_title"])

    def test_relate_survives_non_list_relations_on_the_target(self) -> None:
        _, source_id = self.capture("Quelle")
        target_path, target_id = self.capture("Ziel")
        self.set_field(target_path, "relations", 7)

        related = self.run_cli("relate", source_id, "related_to", target_id)
        self.assertNoTraceback(related)
        self.assertEqual(related.returncode, 0, related.stderr)
        self.assertEqual(json.loads(related.stdout)["action"], "added")


if __name__ == "__main__":
    unittest.main()

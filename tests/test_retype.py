"""Correcting the type of an entry that was filed under the wrong one.

Every type added after a vault exists leaves entries behind under the type
they were given: recipes captured before the `recipe` type existed stayed
notes, and no amount of searching for `type: recipe` finds them. `update`
refuses to touch `type` on purpose, because the type decides both the storage
path and which attributes are legal.

What these tests hold `retype` to is that the correction never costs anything
else about the entry -- the stable ID, its creation time, its relations,
attachments, tags and body all survive a move that changes the directory.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest

from noetrail.frontmatter import parse_frontmatter
from tests import CLI_COMMAND, temporary_root


class RetypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.run_cli("init")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def failing_cli(self, *arguments: str) -> str:
        completed = subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return completed.stderr

    def capture_note(self, title: str = "Spinat-Curry") -> str:
        completed = self.run_cli(
            "capture", "--type", "note", "--title", title, "--text", "Ein Rezept"
        )
        return str(json.loads(completed.stdout)["id"])

    def entry_paths(self) -> list[str]:
        return sorted(
            path.relative_to(self.data).as_posix()
            for path in (self.data / "vault").rglob("*.md")
        )

    def retype(self, *arguments: str) -> dict[str, object]:
        return json.loads(self.run_cli("retype", *arguments).stdout)

    def test_a_preview_changes_nothing_on_disk(self) -> None:
        entry_id = self.capture_note()
        before = self.entry_paths()
        plan = self.retype(entry_id, "--to", "recipe")
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["from"]["type"], "note")
        self.assertEqual(plan["to"]["type"], "recipe")
        self.assertTrue(plan["to"]["path"].startswith("vault/recipes/"))
        self.assertEqual(self.entry_paths(), before)

    def test_applying_moves_the_file_and_keeps_the_entry_intact(self) -> None:
        entry_id = self.capture_note()
        self.run_cli("tag", entry_id, "abendessen")
        other = self.capture_note("Bewährter Pizzateig")
        self.run_cli("relate", entry_id, "related_to", other)

        source = self.data / next(
            path for path in self.entry_paths() if entry_id[3:11] in path
        )
        before, before_body = parse_frontmatter(source)

        plan = self.retype(entry_id, "--to", "recipe", "--apply")
        self.assertFalse(plan["dry_run"])
        self.assertIn("revision", plan)

        moved = self.data / plan["to"]["path"]
        self.assertTrue(moved.exists())
        self.assertFalse(source.exists())
        after, after_body = parse_frontmatter(moved)

        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["tags"], before["tags"])
        self.assertEqual(after["relations"], before["relations"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["sensitivity"], before["sensitivity"])
        self.assertEqual(after_body, before_body)

        self.assertEqual(after["type"], "recipe")
        self.assertEqual(after["type_version"], 1)
        self.assertEqual(after["attributes"], {"interest_status": "none"})
        self.assertNotEqual(after["updated_at"], before["updated_at"])

    def test_the_entry_is_findable_under_its_new_type(self) -> None:
        entry_id = self.capture_note()
        self.assertEqual(
            json.loads(self.run_cli("search", "--type", "recipe", "").stdout)["total"],
            0,
        )
        self.retype(entry_id, "--to", "recipe", "--apply")
        found = json.loads(self.run_cli("search", "--type", "recipe", "").stdout)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["id"], entry_id)
        self.run_cli("validate")

    def test_a_type_whose_required_fields_cannot_be_met_is_refused(self) -> None:
        # `product_kind` is required and has no default, so nothing can silently
        # become a product. The entry must stay exactly where it was.
        entry_id = self.capture_note()
        before = self.entry_paths()
        message = self.failing_cli("retype", entry_id, "--to", "product", "--apply")
        self.assertIn("product", message)
        self.assertEqual(self.entry_paths(), before)

    def test_attributes_the_target_does_not_define_are_not_dropped_silently(
        self,
    ) -> None:
        completed = self.run_cli(
            "capture",
            "--type",
            "product",
            "--product-kind",
            "rum",
            "--title",
            "Rum aus Barbados",
            "--text",
            "Notiz",
        )
        entry_id = str(json.loads(completed.stdout)["id"])
        before = self.entry_paths()

        message = self.failing_cli("retype", entry_id, "--to", "note", "--apply")
        self.assertIn("product_kind", message)
        self.assertIn("--drop-unsupported", message)
        self.assertEqual(self.entry_paths(), before)

        plan = self.retype(
            entry_id, "--to", "note", "--drop-unsupported", "--apply"
        )
        self.assertIn("product_kind", plan["attributes"]["dropped"])
        moved = self.data / plan["to"]["path"]
        metadata, _ = parse_frontmatter(moved)
        self.assertEqual(metadata["type"], "note")
        self.assertNotIn("product_kind", metadata["attributes"])

    def test_retyping_to_the_same_type_is_refused(self) -> None:
        entry_id = self.capture_note()
        message = self.failing_cli("retype", entry_id, "--to", "note", "--apply")
        self.assertIn("already", message)

    def test_a_stale_revision_is_refused(self) -> None:
        entry_id = self.capture_note()
        stale = "sha256:" + "0" * 64
        message = self.failing_cli(
            "retype",
            entry_id,
            "--to",
            "recipe",
            "--expected-revision",
            stale,
            "--apply",
        )
        self.assertIn("Revision conflict", message)
        self.assertTrue(
            any(path.startswith("vault/notes/") for path in self.entry_paths())
        )

    def test_an_unknown_target_type_is_refused(self) -> None:
        entry_id = self.capture_note()
        message = self.failing_cli("retype", entry_id, "--to", "invented", "--apply")
        self.assertTrue(message.strip())
        self.assertTrue(
            any(path.startswith("vault/notes/") for path in self.entry_paths())
        )

    def test_the_relation_from_another_entry_still_resolves(self) -> None:
        # Relations reference stable IDs, not paths, so a move must leave the
        # graph intact. This is the check that the ID really did survive.
        entry_id = self.capture_note()
        other = self.capture_note("Bewährter Pizzateig")
        self.run_cli("relate", other, "related_to", entry_id)
        self.retype(entry_id, "--to", "recipe", "--apply")
        self.run_cli("validate")
        relations = json.loads(self.run_cli("relations", entry_id).stdout)
        self.assertGreaterEqual(json.dumps(relations).find(other), 0)


if __name__ == "__main__":
    unittest.main()

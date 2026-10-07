from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class CustomTypeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.inbox = self.base / "attachment-inbox"
        for path in (self.data, self.config, self.inbox):
            path.mkdir()

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
                "--attachment-inbox",
                str(self.inbox),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def write_json(self, name: str, value: object) -> Path:
        path = self.base / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def metadata(path: Path) -> dict[str, object]:
        lines = path.read_text(encoding="utf-8").splitlines()
        end = lines[1:].index("---") + 1
        return {
            key: json.loads(value.strip())
            for key, value in (
                line.split(":", 1) for line in lines[1:end]
            )
        }

    def test_books_pack_supports_complete_generic_lifecycle(self) -> None:
        listed = self.run_cli("schema", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(
            "books/book",
            {item["id"] for item in json.loads(listed.stdout)["types"]},
        )

        missing_attributes = self.write_json("missing.json", {})
        missing = self.run_cli(
            "capture",
            "--type",
            "books/book",
            "--title",
            "Missing author",
            "--text",
            "Synthetic body.",
            "--attributes-file",
            str(missing_attributes),
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing required field author", missing.stderr)
        self.assertEqual(list(self.data.rglob("*.md")), [])

        invalid_attributes = self.write_json(
            "invalid.json",
            {"author": "Ursula K. Le Guin", "reading_state": "later"},
        )
        invalid = self.run_cli(
            "capture",
            "--type",
            "books/book",
            "--title",
            "Invalid state",
            "--text",
            "Synthetic body.",
            "--attributes-file",
            str(invalid_attributes),
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("wishlist, reading, read, abandoned", invalid.stderr)

        attributes = self.write_json(
            "book.json",
            {
                "author": "Ursula K. Le Guin",
                "topics": ["anarchism", "science fiction"],
            },
        )
        captured = self.run_cli(
            "capture",
            "--type",
            "books/book",
            "--title",
            "The Dispossessed",
            "--text",
            "",
            "--attributes-file",
            str(attributes),
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        capture_result = json.loads(captured.stdout)
        self.assertTrue(
            capture_result["created"].startswith("vault/custom/books/book/")
        )
        entry_path = self.data / capture_result["created"]
        entry_metadata = self.metadata(entry_path)
        self.assertEqual(entry_metadata["type"], "books/book")
        self.assertEqual(entry_metadata["type_version"], 1)
        self.assertEqual(
            entry_metadata["attributes"]["reading_state"],  # type: ignore[index]
            "wishlist",
        )
        self.assertIn("## Notes", entry_path.read_text(encoding="utf-8"))

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

        selected = self.run_cli("review", capture_result["id"])
        self.assertEqual(selected.returncode, 0, selected.stderr)
        selected_result = json.loads(selected.stdout)
        self.assertEqual(selected_result["type_version"], 1)
        self.assertEqual(
            selected_result["attributes"]["author"],
            "Ursula K. Le Guin",
        )

        first_image = self.inbox / "cover.png"
        first_image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-cover")
        attached = self.run_cli(
            "attach",
            capture_result["id"],
            str(first_image),
            "--expected-revision",
            capture_result["revision"],
            "--caption",
            "Book cover",
        )
        self.assertEqual(attached.returncode, 0, attached.stderr)
        attached_result = json.loads(attached.stdout)

        second_image = self.inbox / "other.png"
        second_image.write_bytes(b"\x89PNG\r\n\x1a\nother-cover")
        stale = self.run_cli(
            "attach",
            capture_result["id"],
            str(second_image),
            "--expected-revision",
            capture_result["revision"],
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("Revision conflict", stale.stderr)

        patch = self.write_json(
            "update.json",
            {"attributes": {"reading_state": "read", "pages": 341}},
        )
        updated = self.run_cli(
            "update",
            capture_result["id"],
            "--patch-file",
            str(patch),
            "--expected-revision",
            attached_result["revision"],
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        update_result = json.loads(updated.stdout)
        self.assertEqual(update_result["changed"], ["attributes"])

        invalid_patch = self.write_json(
            "invalid-update.json",
            {"attributes": {"pages": 0}},
        )
        invalid_update = self.run_cli(
            "update",
            capture_result["id"],
            "--patch-file",
            str(invalid_patch),
            "--expected-revision",
            update_result["revision"],
        )
        self.assertNotEqual(invalid_update.returncode, 0)
        self.assertIn("must be at least 1", invalid_update.stderr)

        by_author = self.run_cli(
            "search",
            "Le Guin",
            "--type",
            "books/book",
        )
        self.assertEqual(by_author.returncode, 0, by_author.stderr)
        author_results = json.loads(by_author.stdout)["items"]
        self.assertEqual(len(author_results), 1)
        self.assertEqual(
            author_results[0]["attributes"]["reading_state"],
            "read",
        )
        self.assertNotIn("pages", author_results[0]["attributes"])

        non_searchable_text = self.run_cli(
            "search",
            "341",
            "--type",
            "books/book",
        )
        self.assertEqual(json.loads(non_searchable_text.stdout)["items"], [])

        by_state = self.run_cli(
            "search",
            "",
            "--type",
            "books/book",
            "--attribute-filter",
            'reading_state="read"',
        )
        self.assertEqual(by_state.returncode, 0, by_state.stderr)
        self.assertEqual(json.loads(by_state.stdout)["total"], 1)

        forbidden_filter = self.run_cli(
            "search",
            "",
            "--type",
            "books/book",
            "--attribute-filter",
            "pages=341",
        )
        self.assertNotEqual(forbidden_filter.returncode, 0)
        self.assertIn("not declared searchable", forbidden_filter.stderr)

        note = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Related essay",
            "--text",
            "Synthetic note.",
        )
        self.assertEqual(note.returncode, 0, note.stderr)
        note_result = json.loads(note.stdout)
        related = self.run_cli(
            "relate",
            capture_result["id"],
            "related_to",
            note_result["id"],
            "--expected-revision",
            update_result["revision"],
        )
        self.assertEqual(related.returncode, 0, related.stderr)
        relation_result = json.loads(related.stdout)

        trashed = self.run_cli(
            "trash",
            capture_result["id"],
            "--expected-revision",
            relation_result["revision"],
        )
        self.assertEqual(trashed.returncode, 0, trashed.stderr)
        trash_result = json.loads(trashed.stdout)
        hidden = self.run_cli(
            "search",
            "",
            "--type",
            "books/book",
        )
        self.assertEqual(json.loads(hidden.stdout)["items"], [])
        validates_in_trash = self.run_cli("validate")
        self.assertEqual(
            validates_in_trash.returncode,
            0,
            validates_in_trash.stdout,
        )

        restored = self.run_cli(
            "restore",
            capture_result["id"],
            "--expected-revision",
            trash_result["revision"],
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        restored_result = json.loads(restored.stdout)
        final_read = self.run_cli("review", capture_result["id"])
        self.assertEqual(
            json.loads(final_read.stdout)["attachment_count"],
            1,
        )

        restored_path = self.data / restored_result["restored"]
        content = restored_path.read_text(encoding="utf-8")
        restored_path.write_text(
            content.replace('"pages":341', '"pages":0'),
            encoding="utf-8",
        )
        invalid_vault = self.run_cli("validate")
        self.assertNotEqual(invalid_vault.returncode, 0)
        self.assertIn("must be at least 1", invalid_vault.stdout)

    def test_custom_storage_rejects_symlinked_pack_directory(self) -> None:
        custom_root = self.data / "vault" / "custom"
        custom_root.mkdir(parents=True)
        outside = self.base / "outside"
        outside.mkdir()
        (custom_root / "books").symlink_to(
            outside,
            target_is_directory=True,
        )
        attributes = self.write_json(
            "book.json",
            {"author": "Octavia E. Butler"},
        )
        captured = self.run_cli(
            "capture",
            "--type",
            "books/book",
            "--title",
            "Parable of the Sower",
            "--text",
            "Synthetic body.",
            "--attributes-file",
            str(attributes),
        )
        self.assertNotEqual(captured.returncode, 0)
        self.assertIn("must not contain symbolic links", captured.stderr)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

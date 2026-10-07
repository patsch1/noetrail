"""One vault scan per command instead of one per lookup.

Before `VaultIndex`, `find_entry`, `duplicate_titles`, `duplicate_bookmarks`,
`duplicate_recipes` and `validate_relation_objects` each called `load_entries`
for themselves and no caller shared the result. Measured at 10 000 entries:
`tag` 521 ms (one scan), `capture` 979 ms (two), `relate` 980 ms per direction
(three), plus a fourth scan whenever `find_entry` missed and re-walked the
vault only to count damaged files. `search` additionally hashed every match
before the `--limit` slice, so a broad query read all 10 000 files a second
time to print at most 50.

These tests count the scans and the hashes rather than the milliseconds: a
timing assertion is noise on shared hardware, and the number of passes is what
the change is actually about.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from noetrail import cli, entries, store
from noetrail.errors import Conflict, NotFound
from noetrail.layout import resolve_layout
from noetrail.schema import SchemaRegistry
from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class CountingVaultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.assertEqual(self.run_cli("init"), 0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> int:
        """Run one command in this process and return its exit code."""

        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout):
            return cli.run_command(
                [
                    "--data-root",
                    str(self.data),
                    "--config-root",
                    str(self.config),
                    "--builtins-root",
                    str(BUILTINS_ROOT),
                    *arguments,
                ]
            )

    def json_cli(self, *arguments: str) -> dict[str, object]:
        self.assertEqual(self.run_cli(*arguments), 0)
        return json.loads(self.stdout.getvalue())

    @contextlib.contextmanager
    def counted_everywhere(self, module, name: str):
        """Replace one function in every module that imported it by name.

        The package is split across modules that do `from noetrail.store
        import revision_for`, which binds a second reference. Patching only
        the definition site would leave those references untouched and the
        counter would silently record zero calls -- a green test that measures
        nothing.
        """

        original = getattr(module, name)
        calls: list[Path] = []

        def counting(argument, *rest, **keywords):
            calls.append(argument)
            return original(argument, *rest, **keywords)

        holders = [
            candidate
            for candidate in list(sys.modules.values())
            if getattr(candidate, "__name__", "").startswith("noetrail")
            and getattr(candidate, name, None) is original
        ]
        self.assertIn(module, holders)
        with contextlib.ExitStack() as stack:
            for holder in holders:
                stack.enter_context(patch.object(holder, name, counting))
            yield calls

    def counted_scans(self):
        return self.counted_everywhere(entries, "load_entries_with_diagnostics")

    def counted_revisions(self):
        return self.counted_everywhere(store, "revision_for")

    def capture(self, title: str, text: str = "Body text.") -> str:
        created = self.json_cli(
            "capture", "--type", "note", "--title", title, "--text", text
        )
        return str(created["id"])


class ScanCountTest(CountingVaultTest):
    def test_each_mutating_command_walks_the_vault_once(self) -> None:
        first = self.capture("First note")
        second = self.capture("Second note")

        commands = {
            "tag": ("tag", first, "alpha"),
            "capture": (
                "capture",
                "--type",
                "note",
                "--title",
                "Third note",
                "--text",
                "More.",
            ),
            "relate": ("relate", first, "related_to", second),
            "status": ("status", first, "--lifecycle", "archived"),
            "trash": ("trash", second),
        }
        for label, arguments in commands.items():
            with self.subTest(command=label):
                with self.counted_scans() as scans:
                    self.assertEqual(self.run_cli(*arguments), 0)
                self.assertEqual(len(scans), 1, f"{label} scanned {len(scans)}x")

    def test_bookmark_and_recipe_share_one_scan_for_both_checks(self) -> None:
        # Both run a duplicate check and a relation check; each used to load
        # the whole vault for itself.
        for label, arguments in (
            ("bookmark", ("bookmark", "https://index.invalid/a")),
            ("recipe", ("recipe", "https://index.invalid/b")),
        ):
            with self.subTest(command=label):
                with self.counted_scans() as scans:
                    self.assertEqual(self.run_cli(*arguments), 0)
                self.assertEqual(len(scans), 1)

    def test_update_shares_one_scan_across_lookup_and_duplicate_check(
        self,
    ) -> None:
        entry_id = self.capture("Original title")
        self.capture("Another title")
        patch_file = self.base / "patch.json"
        patch_file.write_text(
            json.dumps({"title": "Renamed title"}), encoding="utf-8"
        )
        with self.counted_scans() as scans:
            self.assertEqual(
                self.run_cli(
                    "update", entry_id, "--patch-file", str(patch_file)
                ),
                0,
            )
        self.assertEqual(len(scans), 1)

    def test_a_missing_entry_is_reported_without_a_second_pass(self) -> None:
        self.capture("Present note")
        with self.counted_scans() as scans:
            with self.assertRaises(NotFound) as raised:
                self.run_cli("tag", "kn_" + "0" * 32, "alpha")
        self.assertEqual(len(scans), 1)
        self.assertIn("Entry does not exist", str(raised.exception))
        self.assertEqual(raised.exception.code, "not_found")

    def test_a_damaged_file_is_still_named_in_the_miss_message(self) -> None:
        """The count that used to cost a whole extra scan on the miss path."""

        self.capture("Present note")
        broken = self.data / "vault" / "notes" / "broken.md"
        broken.write_text("no frontmatter here\n", encoding="utf-8")
        with self.counted_scans() as scans:
            with self.assertRaises(NotFound) as raised:
                self.run_cli("tag", "kn_" + "0" * 32, "alpha")
        self.assertEqual(len(scans), 1)
        self.assertIn("1 vault file(s) could not be parsed", str(raised.exception))


class SearchWorkTest(CountingVaultTest):
    def test_only_the_returned_page_is_hashed(self) -> None:
        for index in range(12):
            self.capture(f"Shared subject {index}", "quernstone body text")

        with self.counted_revisions() as revisions:
            page = self.json_cli("search", "quernstone", "--limit", "3")
        self.assertEqual(page["total"], 12)
        self.assertEqual(page["returned"], 3)
        # One hash per returned item, not one per match.
        self.assertEqual(len(revisions), 3)
        self.assertTrue(
            all("revision" in item for item in page["items"])  # type: ignore[union-attr]
        )

    def test_paging_hashes_the_page_it_returns(self) -> None:
        for index in range(6):
            self.capture(f"Paged subject {index}", "quernstone body text")
        with self.counted_revisions() as revisions:
            page = self.json_cli(
                "search", "quernstone", "--limit", "2", "--offset", "4"
            )
        self.assertEqual(len(revisions), 2)
        returned = {item["id"] for item in page["items"]}  # type: ignore[union-attr]
        self.assertEqual(len(returned), 2)

    def test_search_scans_once(self) -> None:
        self.capture("Only note", "quernstone body text")
        with self.counted_scans() as scans:
            self.json_cli("search", "quernstone")
        self.assertEqual(len(scans), 1)


class IndexBehaviourTest(CountingVaultTest):
    """The index must not change any answer, only how often it is computed."""

    def test_duplicate_titles_are_still_refused_per_type(self) -> None:
        self.capture("Same words")
        with self.assertRaises(Conflict) as raised:
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "  same WORDS ",
                "--text",
                "Body.",
            )
        self.assertIn("already exists", str(raised.exception))
        # A different type with the same title is a different entry.
        self.assertEqual(
            self.run_cli(
                "capture",
                "--type",
                "thought",
                "--title",
                "Same words",
                "--text",
                "Body.",
            ),
            0,
        )

    def test_a_duplicated_id_is_refused_instead_of_picked_from(self) -> None:
        entry_id = self.capture("Copied note")
        original = next((self.data / "vault" / "notes").glob("*.md"))
        (original.parent / "copy.md").write_text(
            original.read_text(encoding="utf-8"), encoding="utf-8"
        )
        with self.assertRaises(Conflict) as raised:
            self.run_cli("tag", entry_id, "alpha")
        self.assertIn("duplicated", str(raised.exception))

    def test_an_unreadable_entry_is_reported_by_search(self) -> None:
        self.capture("Readable note", "quernstone body text")
        (self.data / "vault" / "notes" / "broken.md").write_text(
            "not an entry\n", encoding="utf-8"
        )
        page = self.json_cli("search", "quernstone")
        self.assertFalse(page["complete"])
        self.assertEqual(page["unreadable_entry_count"], 1)

    def test_inventory_still_counts_the_files_it_could_not_read(self) -> None:
        self.capture("Counted note")
        (self.data / "vault" / "notes" / "broken.md").write_text(
            "not an entry\n", encoding="utf-8"
        )
        counts = self.json_cli("inventory")
        self.assertEqual(counts["entry_count"], 2)
        self.assertEqual(counts["readable_entry_count"], 1)
        self.assertEqual(counts["unreadable_entry_count"], 1)
        self.assertFalse(counts["complete"])


class RegistryCacheTest(unittest.TestCase):
    def test_the_type_views_are_built_once_per_registry(self) -> None:
        """They are read in the search loop and twice per validated entry."""

        layout = resolve_layout(
            data_root=None,
            root=REPOSITORY_ROOT,
            application_root=REPOSITORY_ROOT,
        )
        registry = SchemaRegistry.load(layout)
        self.assertIs(registry.custom_types, registry.custom_types)
        self.assertIs(registry.builtin_types, registry.builtin_types)
        self.assertEqual(
            set(registry.types),
            set(registry.custom_types) | set(registry.builtin_types),
        )


if __name__ == "__main__":
    unittest.main()

"""A merge is explicit, revision-bound and rolls back on write failure."""

from pathlib import Path
import unittest
from unittest.mock import patch

from noetrail.errors import Conflict, InvalidRequest, RevisionConflict
from noetrail.frontmatter import parse_frontmatter
from noetrail.mcp import NoetrailServer
from noetrail.store import atomic_write_entry
from tests.test_search_index import VaultHarness


class CurateTest(VaultHarness, unittest.TestCase):
    def seed_merge(self):
        source = self.capture("Synthetic coffee", "Original source wording.")
        target = self.capture("Synthetic coffee notes", "Original target wording.")
        third = self.capture("Synthetic project", "A third entry.")
        self.json_cli("relate", third, "related_to", source)
        return source, target, third

    def snapshot(self):
        return {path: path.read_bytes() for path in self.data.rglob("*.md")}

    def test_preview_apply_preserves_source_and_redirects_incoming_edge(self):
        source, target, third = self.seed_merge()
        before = self.snapshot()
        plan = self.json_cli("merge", source, target)
        self.assertEqual(before, self.snapshot())
        self.json_cli(
            "merge", source, target, "--apply", "--expected-plan", plan["expected_plan"]
        )
        merged = self.json_cli("review", target)
        self.assertIn("Original source wording.", merged["body"])
        self.assertIn("Original target wording.", merged["body"])
        self.assertEqual(self.search("Original source")["total"], 1)
        incoming = self.search("--related-id", target, "")
        self.assertEqual([item["id"] for item in incoming["items"]], [third])
        trash = self.json_cli("trash", "list")
        self.assertEqual(trash[0]["id"], source)
        _, original_body = parse_frontmatter(self.data / trash[0]["path"])
        self.assertEqual(original_body.strip(), "Original source wording.")
        self.assertEqual(self.run_cli("validate"), 0, self.stdout.getvalue())

    def test_changed_incoming_entry_invalidates_plan(self):
        source, target, third = self.seed_merge()
        plan = self.json_cli("merge", source, target)
        self.json_cli("tag", third, "changed")
        before = self.snapshot()
        with self.assertRaises(RevisionConflict):
            self.json_cli(
                "merge",
                source,
                target,
                "--apply",
                "--expected-plan",
                plan["expected_plan"],
            )
        self.assertEqual(before, self.snapshot())

    def test_failure_after_first_write_restores_exact_bytes(self):
        source, target, _ = self.seed_merge()
        plan = self.json_cli("merge", source, target)
        before = self.snapshot()
        writes = []

        def fail_second(path: Path, metadata, body):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("synthetic write failure")
            atomic_write_entry(path, metadata, body)

        with patch(
            "noetrail.commands.curate.atomic_write_entry", side_effect=fail_second
        ):
            with self.assertRaises(OSError):
                self.json_cli(
                    "merge",
                    source,
                    target,
                    "--apply",
                    "--expected-plan",
                    plan["expected_plan"],
                )
        self.assertEqual(before, self.snapshot())
        self.assertFalse(list(self.data.rglob(".merge-*")))

    def test_conflicting_metadata_refused_without_changes(self):
        source, target, _ = self.seed_merge()
        patch_file = self.base / "patch.json"
        patch_file.write_text('{"sensitivity":"sensitive"}', encoding="utf-8")
        self.json_cli(
            "update",
            source,
            "--patch-file",
            str(patch_file),
            "--expected-revision",
            self.json_cli("review", source)["revision"],
        )
        before = self.snapshot()
        with self.assertRaises(Conflict):
            self.json_cli("merge", source, target)
        self.assertEqual(before, self.snapshot())

    def test_candidate_suggestions_and_mcp_merge_preview(self):
        source, target, _ = self.seed_merge()
        server = NoetrailServer(self.layout())
        suggestions = server.call_tool("find_candidates", {"id": source})
        self.assertEqual(suggestions["items"][0]["id"], target)
        self.assertNotIn("body", suggestions["items"][0])
        preview = server.call_tool(
            "merge_entries", {"source_id": source, "target_id": target}
        )
        self.assertTrue(preview["dry_run"])
        applied = server.call_tool(
            "merge_entries",
            {
                "source_id": source,
                "target_id": target,
                "apply": True,
                "expected_plan": preview["expected_plan"],
            },
        )
        self.assertFalse(applied["dry_run"])

    def test_fetched_content_and_self_relations_are_refused(self):
        source = self.capture("Fetched", "<!-- noetrail:web-content -->\nweb text")
        target = self.capture("Target", "User text")
        with self.assertRaises(Conflict):
            self.json_cli("merge", source, target)
        clean = self.capture("Clean", "User source")
        self.json_cli("relate", clean, "related_to", target)
        with self.assertRaises(Conflict):
            self.json_cli("merge", clean, target)
        with self.assertRaises(InvalidRequest):
            self.json_cli("merge", target, target)

    def test_missing_plan_and_invalid_candidate_page_are_refused(self):
        source, target, _ = self.seed_merge()
        before = self.snapshot()
        with self.assertRaises(RevisionConflict):
            self.json_cli("merge", source, target, "--apply")
        with self.assertRaises(InvalidRequest):
            self.json_cli("candidates", source, "--limit", "51")
        self.assertEqual(before, self.snapshot())

    def test_source_move_failure_also_rolls_back_prior_edits(self):
        source, target, _ = self.seed_merge()
        plan = self.json_cli("merge", source, target)
        before = self.snapshot()

        def fail_after_rewrite(path, destination, metadata, body):
            atomic_write_entry(path, metadata, body)
            raise OSError("synthetic relocation failure")

        with patch(
            "noetrail.commands.curate.relocate_entry", side_effect=fail_after_rewrite
        ):
            with self.assertRaises(OSError):
                self.json_cli(
                    "merge",
                    source,
                    target,
                    "--apply",
                    "--expected-plan",
                    plan["expected_plan"],
                )
        self.assertEqual(before, self.snapshot())

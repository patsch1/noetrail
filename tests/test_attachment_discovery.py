"""Structural image discovery must work without image words in entry content."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from noetrail.layout import resolve_layout
from noetrail.mcp import NoetrailServer, ToolFailure
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\nsynthetic attachment"


class AttachmentDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.inbox = self.base / "discord_files"
        self.inbox.mkdir()
        self.cli("init")
        self.layout = resolve_layout(
            data_root=self.data,
            config_root=self.config,
            builtins_root=REPOSITORY_ROOT / ".knowledge",
        )
        self.first = self.cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Basalt summit",
            "--text",
            "Basalt summit notes",
        )
        self.second = self.cli(
            "capture",
            "--type",
            "product",
            "--title",
            "Basalt sample",
            "--product-kind",
            "other",
            "--text",
            "Basalt sample details",
        )
        self.without = self.cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Basalt plain",
            "--text",
            "Basalt notes without visual media",
        )
        source = self.inbox / "one.png"
        source.write_bytes(PNG)
        other = self.inbox / "two.png"
        other.write_bytes(PNG + b" second image")
        result = self.cli(
            "attach",
            self.first["id"],
            str(source),
            "--expected-revision",
            self.first["revision"],
        )
        self.cli(
            "attach",
            self.first["id"],
            str(other),
            "--expected-revision",
            result["revision"],
        )
        self.cli(
            "attach",
            self.second["id"],
            str(source),
            "--expected-revision",
            self.second["revision"],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *arguments: str):
        result = subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                "--builtins-root",
                str(REPOSITORY_ROOT / ".knowledge"),
                "--attachment-inbox",
                str(self.inbox),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_all_ranks_filter_before_pagination_with_and_without_cache(self):
        wanted = {self.first["id"], self.second["id"]}
        for indexed in (False, True):
            if indexed:
                self.cli("index", "rebuild")
            for rank in ("substring", "bm25", "hybrid"):
                for query in ("", "Basalt"):
                    with self.subTest(indexed=indexed, rank=rank, query=query):
                        result = self.cli(
                            "search",
                            query,
                            "--has-attachment",
                            "--rank",
                            rank,
                            "--limit",
                            "1",
                            "--sort",
                            "title_asc",
                        )
                        self.assertEqual(result["total"], 2)
                        self.assertTrue(result["has_more"])
                        page = self.cli(
                            "search",
                            query,
                            "--has-attachment",
                            "--rank",
                            rank,
                            "--limit",
                            "1",
                            "--offset",
                            str(result["next_offset"]),
                            "--sort",
                            "title_asc",
                        )
                        items = result["items"] + page["items"]
                        self.assertEqual({item["id"] for item in items}, wanted)
                        self.assertEqual(
                            sum(item["attachment_count"] for item in items), 3
                        )
                        self.assertFalse(page["has_more"])
                        no_image = self.cli(
                            "search", query, "--no-has-attachment", "--rank", rank
                        )
                        self.assertEqual(
                            [item["id"] for item in no_image["items"]],
                            [self.without["id"]],
                        )
                        self.assertEqual(no_image["items"][0]["attachment_count"], 0)
            inventory = self.cli("inventory")
            self.assertTrue(inventory["complete"])
            self.assertEqual(inventory["entries_with_attachments"], 2)
            self.assertEqual(inventory["attachment_count"], 3)

    def test_type_and_fallback_filters_do_not_admit_entries_without_images(self):
        selected = self.cli("search", "Basalt", "--has-attachment", "--type", "note")
        self.assertEqual([item["id"] for item in selected["items"]], [self.first["id"]])
        missing = self.cli("search", "Basalt", "--has-attachment", "--type", "recipe")
        self.assertEqual(missing["total"], 0)
        self.assertEqual(missing["without_type_filter"]["total"], 2)
        # Typo candidates keep the structural filter, including on a fresh index.
        self.cli("index", "rebuild")
        hints = self.cli("search", "baslat", "--has-attachment")
        self.assertEqual(
            {item["id"] for item in hints["items"]},
            {self.first["id"], self.second["id"]},
        )
        self.assertTrue(all(item["match_kind"] == "typo" for item in hints["items"]))

    def test_mcp_boolean_filter_and_retrieval_work_in_both_read_modes(self):
        for in_process in (True, False):
            server = NoetrailServer(self.layout, in_process_reads=in_process)
            for has_attachment in (True, False):
                page = server.call_tool(
                    "search", {"query": "", "has_attachment": has_attachment}
                )
                expected = (
                    {self.first["id"], self.second["id"]}
                    if has_attachment
                    else {self.without["id"]}
                )
                self.assertEqual({item["id"] for item in page["items"]}, expected)
                details = server.call_tool(
                    "retrieve",
                    {
                        "query": "Basalt",
                        "query_variants": ["summit"],
                        "has_attachment": has_attachment,
                        "max_body_chars": 100000,
                    },
                )
                self.assertEqual({item["id"] for item in details["items"]}, expected)
                for item in details["items"]:
                    self.assertEqual(
                        bool(item.get("attachments")), has_attachment
                    )
            inventory = server.call_tool("inventory", {})
            self.assertEqual(inventory["attachment_count"], 3)
            self.assertEqual(inventory["entries_with_attachments"], 2)
            with self.assertRaises(ToolFailure):
                server.call_tool("search", {"query": "", "has_attachment": "true"})
            with self.assertRaises(ToolFailure):
                server.call_tool("retrieve", {"query": "", "has_attachment": 1})

    def test_trash_excluded_and_incomplete_inventory_does_not_claim_absence(self):
        read = self.cli("review", self.second["id"])
        self.cli("trash", self.second["id"], "--expected-revision", read["revision"])
        page = self.cli("search", "", "--has-attachment")
        self.assertEqual([item["id"] for item in page["items"]], [self.first["id"]])
        malformed = self.data / "vault" / "notes" / "unreadable.md"
        malformed.write_text(
            "---\ninvalid frontmatter\n---\nSynthetic\n", encoding="utf-8"
        )
        inventory = self.cli("inventory")
        self.assertFalse(inventory["complete"])
        self.assertGreater(inventory["unreadable_entry_count"], 0)
        self.assertEqual(inventory["entries_with_attachments"], 1)
        self.assertEqual(inventory["attachment_count"], 2)

    def test_discord_photo_marker_is_absolute_and_delivers_stored_bytes(self):
        server = NoetrailServer(
            self.layout,
            attachment_inbox=self.inbox,
            attachment_outbox=self.base / "workspace" / "knowledge_media",
            attachment_delivery_marker_template="[PHOTO:{path}]",
        )
        entry = server.call_tool("get_entry", {"id": self.first["id"]})
        record = entry["attachments"][0]
        result = server.call_tool(
            "get_attachment",
            {
                "id": self.first["id"],
                "attachment_id": record["id"],
            },
        )
        self.assertTrue(Path(result["delivery_path"]).is_absolute())
        self.assertEqual(
            result["delivery_marker"], f"[PHOTO:{result['delivery_path']}]"
        )
        self.assertEqual(Path(result["delivery_path"]).read_bytes(), PNG)

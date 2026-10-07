from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from noetrail.constants import MAX_DELIVERABLE_ATTACHMENT_BYTES
from noetrail.layout import resolve_layout
from noetrail.mcp import NoetrailServer
from tests import MCP_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Magic bytes are what decides the media type, so a header plus filler is a
# supported image as far as the vault is concerned. That keeps the fixtures
# small where size does not matter and lets one test make a file that is
# deliberately too large to hand to a chat channel.
PNG_PIXEL = b"\x89PNG\r\n\x1a\n" + b"a small stored photo"


class NoetrailMcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        (self.root / ".knowledge").mkdir()
        (self.root / ".knowledge" / "SPEC.md").write_text(
            "test", encoding="utf-8"
        )
        (self.root / ".knowledge" / "relation-types.yaml").write_text(
            "relations:\n"
            "  related_to:\n"
            "    symmetric: true\n"
            "  involves:\n"
            "    inverse: experienced_in\n"
            "  took_place_at:\n"
            "    inverse: hosted_experience\n",
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge" / "packs" / "knowledge-core",
            self.root / ".knowledge" / "packs" / "knowledge-core",
        )
        schema_pack = self.root / ".knowledge" / "packs" / "books"
        schema_pack.mkdir(parents=True)
        self.schema_manifest = schema_pack / "pack.yaml"
        self.schema_manifest.write_text(
            'format_version: 1\n'
            'id: "books"\n'
            'version: 1\n'
            'title: "Books"\n'
            'description: "Synthetic MCP discovery pack."\n'
            'types:\n'
            '  book:\n'
            '    title: "Book"\n'
            '    description: "A synthetic book."\n'
            '    fields:\n'
            '      author:\n'
            '        type: "string"\n'
            '        required: true\n'
            '        searchable: true\n'
            '      reading_state:\n'
            '        type: "enum"\n'
            '        values: ["wishlist", "reading", "read"]\n'
            '        default: "wishlist"\n'
            '        searchable: true\n'
            '      pages:\n'
            '        type: "integer"\n'
            '        minimum: 1\n',
            encoding="utf-8",
        )
        self.attachment_inbox = self.root / "matrix_files"
        # The host sends files from its own workspace, not from the vault,
        # so a stored image is copied here to be delivered.
        self.attachment_outbox = self.root / "workspace" / "media"
        self.attachment_inbox.mkdir()
        self.process = subprocess.Popen(
            [
                *MCP_COMMAND,
                "--root",
                str(self.root),
                "--attachment-inbox",
                str(self.attachment_inbox),
                "--attachment-outbox",
                str(self.attachment_outbox),
                "--attachment-delivery-marker-template",
                "[image:{path}]",
                "--attachment-delivery-marker-root",
                str(self.root / "workspace"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def tearDown(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()
        if self.process.stderr:
            self.process.stderr.close()
        self.temporary.cleanup()

    def request(
        self,
        request_id: int,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            return_code = self.process.poll()
            stderr = ""
            if return_code is not None and self.process.stderr is not None:
                stderr = self.process.stderr.read()
            self.fail(
                f"MCP server closed stdout (returncode={return_code}): {stderr}"
            )
        response = json.loads(line)
        self.assertEqual(response["id"], request_id)
        return response

    def call_tool(
        self, request_id: int, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        return self.request(
            request_id,
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    def raw_request(self, line: str) -> dict[str, object]:
        """Send a pre-serialized line, bypassing the JSON encoder."""
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        response_line = self.process.stdout.readline()
        if not response_line:
            return_code = self.process.poll()
            stderr = ""
            if return_code is not None and self.process.stderr is not None:
                stderr = self.process.stderr.read()
            self.fail(
                f"MCP server closed stdout (returncode={return_code}): {stderr}"
            )
        return json.loads(response_line)

    def entry_metadata(self, entry: dict[str, object]) -> dict[str, object]:
        """Read an entry's frontmatter straight off disk."""
        path = self.root / str(entry["created"])
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        metadata: dict[str, object] = {}
        for line in lines[1 : lines.index("---", 1)]:
            key, _, raw_value = line.partition(":")
            metadata[key.strip()] = json.loads(raw_value.strip())
        return metadata

    def capture_note(self, request_id: int, title: str) -> dict[str, object]:
        response = self.call_tool(
            request_id,
            "capture",
            {"type": "note", "title": title, "text": "Inhalt."},
        )
        self.assertFalse(response["result"]["isError"], response)  # type: ignore[index]
        return response["result"]["structuredContent"]  # type: ignore[index]

    def test_option_like_tag_values_cannot_change_the_requested_action(
        self,
    ) -> None:
        """A tag named `--remove` must be stored, not obeyed.

        The MCP server appends the tag list to the CLI argument vector. Without
        an explicit `--` separator argparse parsed a tag that happened to look
        like a flag as that flag, so an "add tags" call silently deleted tags.
        """
        entry = self.capture_note(1, "Argument-Injection")
        added = self.call_tool(
            2,
            "set_tags",
            {
                "id": entry["id"],
                "tags": ["alpha", "--remove"],
                "action": "add",
                "expected_revision": entry["revision"],
            },
        )
        self.assertFalse(added["result"]["isError"], added)  # type: ignore[index]
        content = added["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(content["action"], "added")
        self.assertEqual(sorted(content["tags"]), ["--remove", "alpha"])

        stored = self.entry_metadata(entry)
        self.assertEqual(sorted(stored["tags"]), ["--remove", "alpha"])

    def test_tag_values_cannot_disable_the_expected_revision_check(
        self,
    ) -> None:
        """A literal `--` tag must not push `--expected-revision` out of scope.

        `["tag", id, "--", "gamma", "--expected-revision", rev]` made argparse
        treat the trailing option as positional data, so the optimistic-locking
        check silently accepted any revision.
        """
        entry = self.capture_note(1, "Revision-Bypass")
        response = self.call_tool(
            2,
            "set_tags",
            {
                "id": entry["id"],
                "tags": ["--", "gamma"],
                "action": "add",
                "expected_revision": "sha256:" + "0" * 64,
            },
        )
        self.assertTrue(response["result"]["isError"], response)  # type: ignore[index]
        message = json.dumps(response["result"])  # type: ignore[index]
        self.assertIn("Revision conflict", message)

        self.assertEqual(self.entry_metadata(entry)["tags"], [])

    def test_option_like_search_query_is_treated_as_a_query(self) -> None:
        """`search(query="--help")` returned the CLI help text as a success."""
        response = self.call_tool(1, "search", {"query": "--help"})
        self.assertFalse(response["result"]["isError"], response)  # type: ignore[index]
        content = response["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(content["query"], "--help")
        self.assertEqual(content["total"], 0)

    def test_deeply_nested_request_is_rejected_without_killing_the_server(
        self,
    ) -> None:
        """`json.loads` raises `RecursionError`, not `JSONDecodeError`.

        Only the latter was caught, so one malformed line ended the stdio
        server and every following request went unanswered.
        """
        payload = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
            '{"name":"search","arguments":{"query":'
            + "[" * 2000
            + "]" * 2000
            + "}}}"
        )
        response = self.raw_request(payload)
        self.assertEqual(response["error"]["code"], -32700)  # type: ignore[index]

        # The server must still be answering afterwards.
        survivor = self.request(2, "ping")
        self.assertEqual(survivor["id"], 2)

    def test_handshake_and_tool_surface_are_zeroclaw_compatible(self) -> None:
        initialized = self.request(
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        self.assertEqual(
            initialized["result"]["protocolVersion"],  # type: ignore[index]
            "2024-11-05",
        )
        self.assertEqual(
            initialized["result"]["serverInfo"]["name"],  # type: ignore[index]
            "noetrail",
        )

        listed = self.request(2, "tools/list", {})
        tools = listed["result"]["tools"]  # type: ignore[index]
        names = {item["name"] for item in tools}
        self.assertIn("capture", names)
        self.assertIn("list_types", names)
        self.assertIn("describe_type", names)
        self.assertIn("inventory", names)
        self.assertIn("save_recipe", names)
        self.assertIn("save_bookmark", names)
        self.assertIn("list_pending_attachments", names)
        self.assertIn("trash", names)
        self.assertNotIn("purge", names)
        self.assertNotIn("migrate", names)
        self.assertNotIn("shell", names)
        by_name = {item["name"]: item for item in tools}
        attachment_schema = by_name["add_attachment"]["inputSchema"]
        self.assertIn("source_path", attachment_schema["properties"])
        self.assertIn("attachment_token", attachment_schema["properties"])
        self.assertNotIn("source_path", attachment_schema["required"])
        self.assertNotIn("attachment_token", attachment_schema["required"])
        capture_schema = by_name["capture"]["inputSchema"]["properties"]
        self.assertIn("experience", capture_schema["type"]["enum"])
        self.assertIn("recipe", capture_schema["type"]["enum"])
        self.assertIn("books/book", capture_schema["type"]["enum"])
        self.assertIn("attributes", capture_schema)
        self.assertIn("experience_kind", capture_schema)
        self.assertIn("cooking", capture_schema["experience_kind"]["enum"])
        search_schema = by_name["search"]["inputSchema"]["properties"]
        self.assertIn("related_id", search_schema)
        self.assertIn("occurred_after", search_schema)
        self.assertIn("offset", search_schema)
        self.assertIn("sort", search_schema)
        self.assertEqual(search_schema["limit"]["maximum"], 50)
        self.assertIn("books/book", search_schema["type"]["enum"])
        self.assertIn("attribute_filters", search_schema)
        self.assertIn(
            "attributes",
            by_name["update"]["inputSchema"]["properties"],
        )
        for name in {
            "update",
            "add_attachment",
            "set_relation",
            "set_unresolved_relation",
            "set_tags",
            "set_status",
            "complete_review",
            "trash",
            "restore",
        }:
            self.assertIn(
                "expected_revision",
                by_name[name]["inputSchema"]["required"],
            )

        listed_types = self.call_tool(3, "list_types", {})
        type_result = listed_types["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(type_result["type_count"], 13)
        self.assertIn(
            "books/book",
            {item["id"] for item in type_result["types"]},
        )

        described = self.call_tool(
            4,
            "describe_type",
            {"type_id": "books/book"},
        )
        description = described["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(description["id"], "books/book")
        self.assertEqual(
            description["json_schema"]["properties"]["type"],
            {"const": "books/book"},
        )

        self.schema_manifest.write_text(
            self.schema_manifest.read_text(encoding="utf-8")
            + '  magazine:\n'
            + '    title: "Magazine"\n'
            + '    description: "Added after MCP startup."\n',
            encoding="utf-8",
        )
        unchanged_tools = self.request(5, "tools/list", {})
        unchanged_by_name = {
            item["name"]: item
            for item in unchanged_tools["result"]["tools"]  # type: ignore[index]
        }
        unchanged_types = unchanged_by_name["capture"]["inputSchema"][
            "properties"
        ]["type"]["enum"]
        self.assertNotIn("books/magazine", unchanged_types)
        unchanged_registry = self.call_tool(6, "list_types", {})
        self.assertEqual(
            unchanged_registry["result"]["structuredContent"]["type_count"],  # type: ignore[index]
            13,
        )

    def test_discovery_era_profile_uses_modern_handshake(self) -> None:
        metadata = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        discovered = self.request(50, "server/discover", {"_meta": metadata})
        result = discovered["result"]
        self.assertEqual(result["resultType"], "complete")  # type: ignore[index]
        self.assertIn("2024-11-05", result["supportedVersions"])  # type: ignore[index]
        self.assertIn("2026-07-28", result["supportedVersions"])  # type: ignore[index]

        listed = self.request(51, "tools/list", {"_meta": metadata})
        listing = listed["result"]
        self.assertEqual(listing["resultType"], "complete")  # type: ignore[index]
        self.assertEqual(listing["cacheScope"], "private")  # type: ignore[index]
        self.assertIn(
            "search",
            {tool["name"] for tool in listing["tools"]},  # type: ignore[index]
        )

        called = self.request(
            52,
            "tools/call",
            {"_meta": metadata, "name": "inventory", "arguments": {}},
        )
        self.assertEqual(called["result"]["resultType"], "complete")  # type: ignore[index]
        self.assertFalse(called["result"]["isError"])  # type: ignore[index]

    def test_claude_desktop_profile_negotiates_initialize_protocol(self) -> None:
        initialized = self.request(
            54,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "claude-desktop-smoke", "version": "1"},
            },
        )
        self.assertEqual(
            initialized["result"]["protocolVersion"],  # type: ignore[index]
            "2024-11-05",
        )
        listed = self.request(55, "tools/list", {})
        self.assertIn(
            "inventory",
            {
                tool["name"]
                for tool in listed["result"]["tools"]  # type: ignore[index]
            },
        )

    def test_modern_profile_rejects_an_unnegotiated_version(self) -> None:
        response = self.request(
            53,
            "server/discover",
            {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        )
        self.assertEqual(response["error"]["code"], -32022)  # type: ignore[index]
        self.assertEqual(
            response["error"]["data"]["requested"],  # type: ignore[index]
            "2099-01-01",
        )

        malformed = self.request(
            56,
            "tools/list",
            {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                }
            },
        )
        self.assertEqual(malformed["error"]["code"], -32602)  # type: ignore[index]

    def test_inventory_returns_complete_aggregates_without_entry_content(
        self,
    ) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "thought",
                "title": "Inventory-private title",
                "text": "Inventory-private body",
            },
        )
        self.assertFalse(captured["result"]["isError"])  # type: ignore[index]

        inventoried = self.call_tool(2, "inventory", {})
        self.assertFalse(inventoried["result"]["isError"])  # type: ignore[index]
        result = inventoried["result"]["structuredContent"]  # type: ignore[index]
        self.assertTrue(result["complete"])
        self.assertEqual(result["entry_count"], 1)
        self.assertEqual(result["by_type"], {"thought": 1})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Inventory-private title", serialized)
        self.assertNotIn("Inventory-private body", serialized)

    def test_saved_views_are_listed_and_run_through_mcp(self) -> None:
        (self.root / ".knowledge/views.yaml").write_text(
            "format_version: 1\n"
            "views:\n"
            "  notes:\n"
            '    title: "All notes"\n'
            '    query: ""\n'
            '    type: "note"\n'
            "    limit: 10\n",
            encoding="utf-8",
        )
        captured = self.call_tool(
            1,
            "capture",
            {"type": "note", "title": "View result", "text": "Synthetic."},
        )
        self.assertFalse(captured["result"]["isError"])  # type: ignore[index]
        listed = self.call_tool(2, "list_views", {})
        summary = listed["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(summary["views"][0]["name"], "notes")
        run = self.call_tool(3, "run_view", {"name": "notes"})
        page = run["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["title"], "View result")

    def test_search_returns_stable_compact_pages_with_totals(self) -> None:
        for request_id, title in enumerate(
            ("MCP Charlie", "MCP Alpha", "MCP Bravo"),
            start=1,
        ):
            captured = self.call_tool(
                request_id,
                "capture",
                {
                    "type": "thought",
                    "title": title,
                    "text": "MCP pagination marker body.",
                },
            )
            self.assertFalse(captured["result"]["isError"])  # type: ignore[index]

        first = self.call_tool(
            4,
            "search",
            {
                "query": "pagination marker",
                "sort": "title_asc",
                "limit": 2,
            },
        )
        first_page = first["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(first_page["returned"], 2)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["next_offset"], 2)
        self.assertEqual(
            [item["title"] for item in first_page["items"]],
            ["MCP Alpha", "MCP Bravo"],
        )
        self.assertNotIn("path", first_page["items"][0])
        self.assertNotIn("relations", first_page["items"][0])
        self.assertNotIn(
            "pagination marker body",
            json.dumps(first_page["items"], ensure_ascii=False).casefold(),
        )

        second = self.call_tool(
            5,
            "search",
            {
                "query": "pagination marker",
                "sort": "title_asc",
                "limit": 2,
                "offset": first_page["next_offset"],
            },
        )
        second_page = second["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(second_page["total"], 3)
        self.assertEqual(second_page["returned"], 1)
        self.assertFalse(second_page["has_more"])
        self.assertEqual(second_page["items"][0]["title"], "MCP Charlie")

    def test_search_exposes_the_ranking_modes_and_defaults_to_hybrid(
        self,
    ) -> None:
        """The default matters more here than anywhere else.

        An agent asks in the user's words, which is where a literal scan is
        weakest: two words that never occur adjacently used to return nothing
        and be reported as "you have none". The default an agent gets when it
        omits `rank` is therefore the union, and both single modes stay
        reachable for a caller that wants exactly one of them.
        """

        for request_id, (title, text) in enumerate(
            (
                ("Espresso grind", "A finer grind raises extraction."),
                ("Bitter shots", "The shots tasted bitter after the new bag."),
            ),
            start=1,
        ):
            self.call_tool(
                request_id, "capture", {"type": "note", "title": title, "text": text}
            )

        schema = self.call_tool(10, "search", {"query": "x"})
        self.assertFalse(schema["result"]["isError"])  # type: ignore[index]

        # Two words that never occur adjacently.
        default = self.call_tool(11, "search", {"query": "extraction bitter"})
        page = default["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["sort"], "relevance")

        literal = self.call_tool(
            12, "search", {"query": "extraction bitter", "rank": "substring"}
        )
        self.assertEqual(
            literal["result"]["structuredContent"]["total"],  # type: ignore[index]
            0,
        )
        ranked = self.call_tool(
            13, "search", {"query": "extraction bitter", "rank": "bm25"}
        )
        self.assertEqual(
            ranked["result"]["structuredContent"]["total"],  # type: ignore[index]
            2,
        )

    def test_retrieve_returns_full_entries_in_one_bounded_tool_call(self) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "note",
                "title": "Bundled retrieval",
                "text": "Synthetic full body for one-call retrieval.",
            },
        )
        self.assertFalse(captured["result"]["isError"])  # type: ignore[index]

        retrieved = self.call_tool(
            2,
            "retrieve",
            {
                "query": "one-call retrieval",
                "limit": 1,
                "max_body_chars": 100,
            },
        )
        self.assertFalse(retrieved["result"]["isError"])  # type: ignore[index]
        result = retrieved["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(result["mode"], "retrieve")
        self.assertEqual(result["total"], 1)
        self.assertIn("Synthetic full body", result["items"][0]["body"])
        self.assertFalse(result["items"][0]["body_truncated"])

        refused = self.call_tool(
            3,
            "retrieve",
            {"query": "retrieval", "limit": 11},
        )
        self.assertTrue(refused["result"]["isError"])  # type: ignore[index]

    def test_agent_can_capture_and_replace_grounded_aliases(self) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "note",
                "title": "Synthetic Long Name",
                "aliases": ["SLN"],
                "text": "Alias MCP marker.",
            },
        )
        result = captured["result"]["structuredContent"]  # type: ignore[index]
        selected = self.call_tool(2, "get_entry", {"id": result["id"]})
        entry = selected["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(entry["aliases"], ["SLN"])

        updated = self.call_tool(
            3,
            "update",
            {
                "id": result["id"],
                "expected_revision": entry["revision"],
                "aliases": ["Synthetic LN"],
            },
        )
        self.assertFalse(  # type: ignore[index]
            updated["result"]["isError"],
            updated,
        )
        found = self.call_tool(4, "search", {"query": "Synthetic LN"})
        page = found["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(page["total"], 1)

    def test_custom_capture_update_and_search_share_registry_validation(
        self,
    ) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "books/book",
                "title": "A Wizard of Earthsea",
                "attributes": {"author": "Ursula K. Le Guin"},
            },
        )
        self.assertFalse(captured["result"]["isError"])  # type: ignore[index]
        capture_result = captured["result"]["structuredContent"]  # type: ignore[index]
        self.assertTrue(
            capture_result["created"].startswith(
                "vault/custom/books/book/"
            )
        )

        selected = self.call_tool(
            2,
            "get_entry",
            {"id": capture_result["id"]},
        )
        selected_result = selected["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            selected_result["attributes"]["reading_state"],
            "wishlist",
        )

        updated = self.call_tool(
            3,
            "update",
            {
                "id": capture_result["id"],
                "expected_revision": capture_result["revision"],
                "attributes": {"reading_state": "read", "pages": 205},
            },
        )
        self.assertFalse(updated["result"]["isError"])  # type: ignore[index]

        invalid = self.call_tool(
            4,
            "update",
            {
                "id": capture_result["id"],
                "expected_revision": updated["result"]["structuredContent"]["revision"],  # type: ignore[index]
                "attributes": {"pages": 0},
            },
        )
        self.assertTrue(invalid["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "must be at least 1",
            invalid["result"]["content"][0]["text"],  # type: ignore[index]
        )

        searched = self.call_tool(
            5,
            "search",
            {
                "query": "",
                "type": "books/book",
                "attribute_filters": {"reading_state": "read"},
            },
        )
        self.assertFalse(searched["result"]["isError"])  # type: ignore[index]
        search_result = searched["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(search_result["total"], 1)
        self.assertEqual(search_result["returned"], 1)
        self.assertEqual(search_result["items"][0]["id"], capture_result["id"])

        validated = self.call_tool(6, "validate", {})
        self.assertFalse(validated["result"]["isError"])  # type: ignore[index]

    def _entry_with_photo(self, image: bytes, name: str = "photo.png"):
        """Capture an entry and attach one image to it."""

        photo = self.attachment_inbox / name
        photo.write_bytes(image)
        listed = self.call_tool(
            90,
            "list_pending_attachments",
            {"max_age_seconds": 900, "limit": 8},
        )["result"]["structuredContent"]  # type: ignore[index]
        token = next(
            a["attachment_token"]
            for a in listed["attachments"]
            if a["original_name"] == name
        )
        captured = self.call_tool(
            91,
            "capture",
            {"type": "note", "title": "Kaffee", "text": "Synthetic test entry."},
        )["result"]["structuredContent"]  # type: ignore[index]
        attached = self.call_tool(
            92,
            "add_attachment",
            {
                "id": captured["id"],
                "expected_revision": captured["revision"],
                "attachment_token": token,
            },
        )["result"]["structuredContent"]  # type: ignore[index]
        entry = self.call_tool(93, "get_entry", {"id": captured["id"]})
        record = entry["result"]["structuredContent"]["attachments"][-1]  # type: ignore[index]
        return captured["id"], record, attached

    def test_a_stored_photo_can_be_shown_again(self) -> None:
        """Storing a photo the user can never see again is a poor bargain.

        With an outbox configured the host sends the file itself, so the
        response carries the path and not the pixels. Returning both put a
        base64 image into a prompt that only needed a path, and the provider
        refused the request: "Stream must be set to true".
        """

        image = PNG_PIXEL
        entry_id, record, _ = self._entry_with_photo(image)

        shown = self.call_tool(
            1,
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )["result"]  # type: ignore[index]
        self.assertFalse(shown["isError"])
        self.assertNotIn(
            "image",
            {block["type"] for block in shown["content"]},
        )
        self.assertNotIn(
            base64.b64encode(image).decode("ascii"),
            json.dumps(shown),
        )
        # The summary travels with it, so a client that cannot render an image
        # still learns what it was handed. It may name the delivered copy, but
        # never where the vault keeps the blob: that path is the vault's own
        # business and nothing outside it should learn to address one.
        summary = shown["structuredContent"]
        self.assertEqual(summary["size_bytes"], len(image))
        self.assertEqual(summary["attachment_id"], record["id"])
        self.assertNotIn("vault/attachments", json.dumps(summary))
        self.assertNotIn(record["path"], json.dumps(summary))
        self.assertEqual(
            summary["delivery_marker"],
            f'[image:media/{Path(summary["delivery_path"]).name}]',
        )
        self.assertIn(summary["delivery_marker"], shown["content"][0]["text"])

    def test_without_an_outbox_the_image_itself_is_returned(self) -> None:
        """The portable behaviour must survive the delivery shortcut.

        A client that renders MCP image content needs the bytes; only a host
        that sends the file itself does not. Dropping them whenever an outbox
        happens to be configured must not quietly become dropping them always,
        so this runs a server that has none.
        """

        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "portable.png")
        plain = subprocess.Popen(
            [
                *MCP_COMMAND,
                "--root",
                str(self.root),
                "--attachment-inbox",
                str(self.attachment_inbox),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        try:
            assert plain.stdin is not None and plain.stdout is not None
            for payload in (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_attachment",
                        "arguments": {
                            "id": entry_id,
                            "attachment_id": record["id"],
                        },
                    },
                },
            ):
                plain.stdin.write(json.dumps(payload) + "\n")
                plain.stdin.flush()
                response = json.loads(plain.stdout.readline())
        finally:
            plain.stdin.close()  # type: ignore[union-attr]
            plain.wait(timeout=5)
            plain.stdout.close()  # type: ignore[union-attr]
            plain.stderr.close()  # type: ignore[union-attr]

        shown = response["result"]
        self.assertFalse(shown["isError"])
        blocks = {block["type"]: block for block in shown["content"]}
        self.assertEqual(blocks["image"]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(blocks["image"]["data"]), PNG_PIXEL)
        self.assertNotIn("delivery_path", shown["structuredContent"])
        self.assertNotIn("delivery_marker", shown["structuredContent"])

    def test_the_image_is_placed_where_the_host_can_send_it_from(self) -> None:
        """Returning image content is the portable answer; it is not enough here.

        This deployment's Matrix channel uploads a file only when the reply
        names a path inside the workspace it sends from, and the vault is not
        in that workspace. Without the copy the agent has an image it cannot
        deliver, which is what a user sees as "it did not work".
        """

        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "delivered.png")
        summary = self.call_tool(
            10,
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )["result"]["structuredContent"]  # type: ignore[index]

        delivered = Path(summary["delivery_path"])
        self.assertTrue(delivered.is_file())
        self.assertEqual(delivered.read_bytes(), PNG_PIXEL)
        self.assertTrue(delivered.is_relative_to(self.attachment_outbox))
        # Named by content, so the vault's own record and the delivered copy
        # cannot drift apart, and no personal filename spreads outside it.
        self.assertEqual(delivered.stem, record["sha256"])

    def test_an_outbox_does_not_assume_one_hosts_marker_syntax(self) -> None:
        """A path alone remains the generic outbox contract."""

        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "generic.png")
        generic = NoetrailServer(
            resolve_layout(root=str(self.root)),
            attachment_inbox=self.attachment_inbox,
            attachment_outbox=self.root / "generic-outbox",
        )
        summary = generic.call_tool(
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )

        self.assertIn("delivery_path", summary)
        self.assertNotIn("delivery_marker", summary)

    def test_a_marker_root_returns_a_safe_workspace_relative_path(self) -> None:
        """A host can keep a functional marker out of provider image input."""

        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "relative.png")
        workspace = self.root / "relative-workspace"
        relative = NoetrailServer(
            resolve_layout(root=str(self.root)),
            attachment_inbox=self.attachment_inbox,
            attachment_outbox=workspace / "media",
            attachment_delivery_marker_template="[IMAGE:{path}]",
            attachment_delivery_marker_root=workspace,
        )
        summary = relative.call_tool(
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )

        self.assertTrue(Path(summary["delivery_path"]).is_absolute())
        self.assertEqual(
            summary["delivery_marker"],
            f"[IMAGE:media/{record['sha256']}.png]",
        )

    def test_a_marker_without_a_root_keeps_the_absolute_path_contract(
        self,
    ) -> None:
        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "absolute.png")
        absolute = NoetrailServer(
            resolve_layout(root=str(self.root)),
            attachment_inbox=self.attachment_inbox,
            attachment_outbox=self.root / "absolute-outbox",
            attachment_delivery_marker_template="[host:{path}]",
        )
        summary = absolute.call_tool(
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )

        self.assertEqual(
            summary["delivery_marker"],
            f"[host:{summary['delivery_path']}]",
        )

    def test_asking_twice_delivers_one_file(self) -> None:
        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "twice.png")
        for request_id in (11, 12):
            self.call_tool(
                request_id,
                "get_attachment",
                {"id": entry_id, "attachment_id": record["id"]},
            )
        self.assertEqual(
            sorted(p.name for p in self.attachment_outbox.iterdir()),
            [f"{record['sha256']}.png"],
        )

    def test_an_attachment_of_another_entry_is_refused(self) -> None:
        """The entry read is the authorisation, not a formality.

        Without this the tool would be a file reader that happens to take two
        arguments: name any entry, name any attachment, receive any blob.
        """

        _, mine, _ = self._entry_with_photo(PNG_PIXEL, "mine.png")
        other = self.call_tool(
            2,
            "capture",
            {"type": "note", "title": "Unrelated", "text": "Synthetic."},
        )["result"]["structuredContent"]  # type: ignore[index]

        refused = self.call_tool(
            3,
            "get_attachment",
            {"id": other["id"], "attachment_id": mine["id"]},
        )["result"]  # type: ignore[index]
        self.assertTrue(refused["isError"])
        self.assertIn("no attachment", refused["structuredContent"]["error"]["message"])

    def test_an_unknown_attachment_is_refused(self) -> None:
        entry_id, _, _ = self._entry_with_photo(PNG_PIXEL, "known.png")
        refused = self.call_tool(
            4,
            "get_attachment",
            {"id": entry_id, "attachment_id": "ka_" + "0" * 32},
        )["result"]  # type: ignore[index]
        self.assertTrue(refused["isError"])

    def test_a_blob_that_no_longer_matches_its_record_is_refused(self) -> None:
        """A digest mismatch is exactly when handing over bytes matters."""

        entry_id, record, _ = self._entry_with_photo(PNG_PIXEL, "swapped.png")
        blob = self.root / record["path"]
        blob.write_bytes(b"\x89PNG\r\n\x1a\nsomething else entirely")

        refused = self.call_tool(
            5,
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )["result"]  # type: ignore[index]
        self.assertTrue(refused["isError"])
        self.assertIn(
            "does not match",
            refused["structuredContent"]["error"]["message"],
        )

    def test_an_image_too_large_to_deliver_is_refused_not_truncated(self) -> None:
        """The vault stores more than a chat message should carry.

        The refusal has to say so, because the entry is fine and only the
        delivery is not -- a truncated image would be worse than either.
        """

        oversized = b"\x89PNG\r\n\x1a\n" + os.urandom(
            MAX_DELIVERABLE_ATTACHMENT_BYTES + 1024
        )
        entry_id, record, _ = self._entry_with_photo(oversized, "big.png")

        refused = self.call_tool(
            6,
            "get_attachment",
            {"id": entry_id, "attachment_id": record["id"]},
        )["result"]  # type: ignore[index]
        self.assertTrue(refused["isError"])
        message = refused["structuredContent"]["error"]["message"]
        self.assertIn("remains stored", message)
        self.assertIn(str(len(oversized)), message)

    def test_a_photo_delivered_twice_is_one_candidate(self) -> None:
        """The reported failure: one photo, two files, no attachment at all.

        A Matrix delivery wrote the same image to the inbox twice -- byte for
        byte identical, differing only in the prefix the inbox puts on a name.
        Listed as two candidates for one message, the count no longer matched
        the photo, the caller is told to refuse an ambiguous mapping, and the
        user got "I could not identify the photo" instead of an attachment.

        There was never a choice to make. Blobs are stored under their digest,
        so either copy produces the same attachment.
        """

        now = time.time()
        image = b"\xff\xd8\xff" + b"the same photo, delivered twice"
        first = self.attachment_inbox / "aaaa1111_IMG_20260808.jpg"
        first.write_bytes(image)
        os.utime(first, (now - 30, now - 30))
        second = self.attachment_inbox / "bbbb2222_IMG_20260808.jpg"
        second.write_bytes(image)
        os.utime(second, (now - 29, now - 29))
        other = self.attachment_inbox / "cccc3333_other.png"
        other.write_bytes(b"\x89PNG\r\n\x1a\na different photo")
        os.utime(other, (now - 28, now - 28))

        listed = self.call_tool(
            1,
            "list_pending_attachments",
            {"max_age_seconds": 900, "limit": 8},
        )
        pending = listed["result"]["structuredContent"]  # type: ignore[index]

        # Two distinct images, not three candidates.
        self.assertEqual(pending["count"], 2)
        duplicated, distinct = pending["attachments"]
        self.assertEqual(duplicated["duplicate_count"], 2)
        self.assertEqual(distinct["duplicate_count"], 1)
        self.assertNotEqual(duplicated["sha256"], distinct["sha256"])
        self.assertRegex(duplicated["sha256"], r"^[0-9a-f]{64}$")

        # The digest is the entry's own content, so a caller can see that the
        # copies were the same image rather than having to trust the count.
        import hashlib

        self.assertEqual(duplicated["sha256"], hashlib.sha256(image).hexdigest())

        # And the surviving token still attaches.
        captured = self.call_tool(
            2,
            "capture",
            {
                "type": "note",
                "title": "Duplicate delivery",
                "text": "Synthetic test entry.",
            },
        )["result"]["structuredContent"]  # type: ignore[index]
        attached = self.call_tool(
            3,
            "add_attachment",
            {
                "id": captured["id"],
                "expected_revision": captured["revision"],
                "attachment_token": duplicated["attachment_token"],
            },
        )
        self.assertFalse(attached["result"]["isError"])  # type: ignore[index]

    def test_pending_attachment_tokens_are_bounded_opaque_and_hash_bound(
        self,
    ) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "note",
                "title": "Pending attachment test",
                "text": "Synthetic test entry.",
            },
        )
        capture_result = captured["result"]["structuredContent"]  # type: ignore[index]

        now = time.time()
        older = self.attachment_inbox / "older.png"
        older.write_bytes(b"\x89PNG\r\n\x1a\nolder-photo")
        os.utime(older, (now - 20, now - 20))
        newer = self.attachment_inbox / "newer.jpg"
        newer.write_bytes(b"\xff\xd8\xffnewer-photo")
        os.utime(newer, (now - 10, now - 10))
        unsupported = self.attachment_inbox / "payload.svg"
        unsupported.write_text("<svg></svg>", encoding="utf-8")
        old = self.attachment_inbox / "old.png"
        old.write_bytes(b"\x89PNG\r\n\x1a\nold-photo")
        os.utime(old, (now - 2_000, now - 2_000))
        linked = self.attachment_inbox / "linked.png"
        linked.symlink_to(older)

        listed = self.call_tool(
            2,
            "list_pending_attachments",
            {"max_age_seconds": 900, "limit": 8},
        )
        self.assertFalse(listed["result"]["isError"])  # type: ignore[index]
        pending = listed["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(pending["count"], 2)
        self.assertEqual(pending["order"], "oldest_first")
        self.assertEqual(
            [item["original_name"] for item in pending["attachments"]],
            ["older.png", "newer.jpg"],
        )
        serialized = json.dumps(pending)
        self.assertNotIn("source_path", serialized)
        self.assertNotIn(str(self.attachment_inbox), serialized)
        first_token = pending["attachments"][0]["attachment_token"]
        second_token = pending["attachments"][1]["attachment_token"]
        self.assertRegex(first_token, r"^kit_[0-9a-f]{32}$")

        attached = self.call_tool(
            3,
            "add_attachment",
            {
                "id": capture_result["id"],
                "expected_revision": capture_result["revision"],
                "attachment_token": first_token,
            },
        )
        self.assertFalse(attached["result"]["isError"])  # type: ignore[index]
        attached_result = attached["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            attached_result["attachment"]["original_name"],
            "older.png",
        )

        reused = self.call_tool(
            4,
            "add_attachment",
            {
                "id": capture_result["id"],
                "expected_revision": attached_result["revision"],
                "attachment_token": first_token,
            },
        )
        self.assertTrue(reused["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "unknown, expired, or already used",
            reused["result"]["content"][0]["text"],  # type: ignore[index]
        )

        newer.write_bytes(b"\xff\xd8\xffchanged-after-listing")
        tampered = self.call_tool(
            5,
            "add_attachment",
            {
                "id": capture_result["id"],
                "expected_revision": attached_result["revision"],
                "attachment_token": second_token,
            },
        )
        self.assertTrue(tampered["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "changed after it was listed",
            tampered["result"]["content"][0]["text"],  # type: ignore[index]
        )

        selected = self.call_tool(
            6,
            "get_entry",
            {"id": capture_result["id"]},
        )
        selected_result = selected["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(selected_result["attachment_count"], 1)

    def test_add_attachment_requires_one_source_selector(self) -> None:
        missing = self.call_tool(
            1,
            "add_attachment",
            {
                "id": "kn_" + "1" * 32,
                "expected_revision": "sha256:" + "2" * 64,
            },
        )
        self.assertTrue(missing["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "exactly one",
            missing["result"]["content"][0]["text"],  # type: ignore[index]
        )

        both = self.call_tool(
            2,
            "add_attachment",
            {
                "id": "kn_" + "1" * 32,
                "expected_revision": "sha256:" + "2" * 64,
                "source_path": "image.png",
                "attachment_token": "kit_" + "3" * 32,
            },
        )
        self.assertTrue(both["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "exactly one",
            both["result"]["content"][0]["text"],  # type: ignore[index]
        )

    def test_capture_search_update_trash_and_restore(self) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "thought",
                "title": "MCP-Gedanke",
                "text": "Über eine strukturierte Schnittstelle gespeichert.",
                "tags": ["mcp"],
            },
        )
        capture_result = captured["result"]["structuredContent"]  # type: ignore[index]
        entry_id = capture_result["id"]
        initial_revision = capture_result["revision"]
        self.assertRegex(entry_id, r"^kn_[0-9a-f]{32}$")

        searched = self.call_tool(
            2,
            "search",
            {"query": "strukturierte Schnittstelle", "limit": 10},
        )
        search_result = searched["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(search_result["items"][0]["id"], entry_id)
        self.assertNotIn(
            "strukturierte Schnittstelle",
            json.dumps(search_result["items"], ensure_ascii=False),
        )

        updated = self.call_tool(
            3,
            "update",
            {
                "id": entry_id,
                "expected_revision": initial_revision,
                "append": "## Ergänzung\n\nNur angehängt.",
            },
        )
        self.assertFalse(updated["result"]["isError"])  # type: ignore[index]
        updated_revision = updated["result"]["structuredContent"]["revision"]  # type: ignore[index]

        stale = self.call_tool(
            4,
            "update",
            {
                "id": entry_id,
                "expected_revision": initial_revision,
                "append": "Diese Änderung ist veraltet.",
            },
        )
        self.assertTrue(stale["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "Revision conflict",
            stale["result"]["content"][0]["text"],  # type: ignore[index]
        )

        rejected = self.call_tool(
            5,
            "update",
            {
                "id": entry_id,
                "expected_revision": updated_revision,
                "replace_body": "Nicht erlaubt.",
            },
        )
        self.assertTrue(rejected["result"]["isError"])  # type: ignore[index]
        self.assertIn(
            "unknown fields",
            rejected["result"]["content"][0]["text"],  # type: ignore[index]
        )

        trashed = self.call_tool(
            6,
            "trash",
            {"id": entry_id, "expected_revision": updated_revision},
        )
        self.assertFalse(trashed["result"]["isError"])  # type: ignore[index]
        trash_revision = trashed["result"]["structuredContent"]["revision"]  # type: ignore[index]
        empty_search = self.call_tool(7, "search", {"query": "MCP-Gedanke"})
        self.assertEqual(
            empty_search["result"]["structuredContent"]["items"],  # type: ignore[index]
            [],
        )

        restored = self.call_tool(
            8,
            "restore",
            {"id": entry_id, "expected_revision": trash_revision},
        )
        self.assertFalse(restored["result"]["isError"])  # type: ignore[index]
        validated = self.call_tool(9, "validate", {})
        self.assertIn(
            "Validation passed",
            validated["result"]["structuredContent"]["message"],  # type: ignore[index]
        )

    def test_bookmark_payload_is_structured_without_temp_file_access(self) -> None:
        saved = self.call_tool(
            1,
            "save_bookmark",
            {
                "url": "https://example.com/article?utm_source=test",
                "title": "Example article",
                "summary": "Eine neutrale Zusammenfassung.",
                "bookmark_kind": "article",
                "fetch_status": "complete",
            },
        )
        self.assertFalse(saved["result"]["isError"])  # type: ignore[index]
        result = saved["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(result["canonical_url"], "https://example.com/article")
        self.assertTrue((self.root / result["created"]).is_file())

        unread = self.call_tool(
            2,
            "search",
            {
                "query": "",
                "type": "bookmark",
                "reading_status": "unread",
                "bookmark_kind": "article",
            },
        )
        unread_result = unread["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(unread_result["total"], 1)
        self.assertEqual(unread_result["items"][0]["bookmark_kind"], "article")

        marked_read = self.call_tool(
            3,
            "set_status",
            {
                "id": result["id"],
                "expected_revision": result["revision"],
                "reading": "read",
            },
        )
        changed = marked_read["result"]["structuredContent"]["changed"]  # type: ignore[index]
        self.assertEqual(changed["reading_status"], "read")
        self.assertIn("read_at", changed)

        read_in_range = self.call_tool(
            4,
            "search",
            {
                "query": "",
                "type": "bookmark",
                "reading_status": "read",
                "read_after": "2020-01-01T00:00:00+00:00",
                "read_before": "2100-01-01T00:00:00+00:00",
            },
        )
        ranged_result = read_in_range["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(ranged_result["total"], 1)

    def test_recipe_link_planning_and_cooking_history(self) -> None:
        saved = self.call_tool(
            1,
            "save_recipe",
            {
                "url": "https://example.com/recipes/pasta?utm_source=chat#top",
                "canonical_url": "https://example.com/recipes/pasta",
                "title": "MCP Pasta al Limone",
                "site_name": "Example Kitchen",
                "page_description": "Source-provided recipe description.",
                "text": "Beim Kochen mehr Zitronenschale verwenden.",
                "interest_status": "planned",
                "servings": "2 Portionen",
                "prep_minutes": 10,
                "cook_minutes": 15,
            },
        )
        self.assertFalse(saved["result"]["isError"])  # type: ignore[index]
        recipe = saved["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(recipe["source_url"], "https://example.com/recipes/pasta")

        duplicate = self.call_tool(
            2,
            "save_recipe",
            {
                "url": "https://example.com/recipes/pasta?utm_campaign=again",
                "title": "Duplicate recipe",
            },
        )
        self.assertTrue(duplicate["result"]["isError"])  # type: ignore[index]

        planned = self.call_tool(
            3,
            "search",
            {
                "query": "",
                "type": "recipe",
                "domain": "example.com",
                "interest_status": "planned",
            },
        )
        results = planned["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            [item["id"] for item in results["items"]],
            [recipe["id"]],
        )
        self.assertEqual(results["items"][0]["prep_minutes"], 10)

        updated = self.call_tool(
            4,
            "update",
            {
                "id": recipe["id"],
                "expected_revision": recipe["revision"],
                "servings": "3 Portionen",
                "cook_minutes": 20,
            },
        )
        self.assertFalse(updated["result"]["isError"])  # type: ignore[index]
        update_result = updated["result"]["structuredContent"]  # type: ignore[index]

        cooked = self.call_tool(
            5,
            "capture",
            {
                "type": "experience",
                "title": "MCP Pasta gekocht",
                "text": "Mehr Zitronenschale war gut.",
                "experience_kind": "cooking",
                "occurred_at": "2026-08-01T18:30:00+02:00",
                "rating": 5,
                "relations": [
                    {"predicate": "involves", "target": recipe["id"]}
                ],
            },
        )
        self.assertFalse(cooked["result"]["isError"])  # type: ignore[index]
        cooking = cooked["result"]["structuredContent"]  # type: ignore[index]

        history = self.call_tool(
            6,
            "search",
            {
                "query": "",
                "type": "experience",
                "experience_kind": "cooking",
                "related_id": recipe["id"],
                "relation_predicate": "involves",
                "min_rating": 5,
            },
        )
        history_results = history["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            [item["id"] for item in history_results["items"]],
            [cooking["id"]],
        )

        completed = self.call_tool(
            7,
            "set_status",
            {
                "id": recipe["id"],
                "expected_revision": update_result["revision"],
                "interest": "none",
            },
        )
        self.assertFalse(completed["result"]["isError"])  # type: ignore[index]

    def test_structured_experience_capture_attachment_and_search(self) -> None:
        product_response = self.call_tool(
            1,
            "capture",
            {
                "type": "product",
                "title": "Structured MCP Rum",
                "text": "Ein Rum.",
                "product_kind": "rum",
            },
        )
        product = product_response["result"]["structuredContent"]  # type: ignore[index]
        place_response = self.call_tool(
            2,
            "capture",
            {
                "type": "place",
                "title": "Structured MCP Bar",
                "text": "Eine Bar.",
                "place_kind": "bar",
            },
        )
        place = place_response["result"]["structuredContent"]  # type: ignore[index]

        captured = self.call_tool(
            3,
            "capture",
            {
                "type": "experience",
                "title": "Structured MCP Tasting",
                "text": "Würzig und ausgewogen.",
                "experience_kind": "tasting",
                "occurred_at": "2026-07-31T21:00:00+02:00",
                "occurred_precision": "datetime",
                "rating": 4,
                "relations": [
                    {"predicate": "involves", "target": product["id"]},
                    {
                        "predicate": "took_place_at",
                        "target": place["id"],
                    },
                ],
            },
        )
        self.assertFalse(captured["result"]["isError"])  # type: ignore[index]
        experience = captured["result"]["structuredContent"]  # type: ignore[index]

        photo = self.attachment_inbox / "structured-mcp.jpg"
        photo.write_bytes(b"\xff\xd8\xffstructured-mcp-photo")
        attached = self.call_tool(
            4,
            "add_attachment",
            {
                "id": experience["id"],
                "expected_revision": experience["revision"],
                "source_path": str(photo),
                "caption": "Foto vom Tasting",
            },
        )
        self.assertFalse(attached["result"]["isError"])  # type: ignore[index]
        attachment_result = attached["result"]["structuredContent"]  # type: ignore[index]

        updated = self.call_tool(
            5,
            "update",
            {
                "id": experience["id"],
                "expected_revision": attachment_result["revision"],
                "experience_kind": "tasting",
                "occurred_at": "2026-07-31T22:00:00+02:00",
                "occurred_precision": "datetime",
                "rating": 5,
            },
        )
        self.assertFalse(updated["result"]["isError"])  # type: ignore[index]
        changed = updated["result"]["structuredContent"]["changed"]  # type: ignore[index]
        self.assertIn("occurred_at", changed)
        self.assertIn("rating", changed)

        found = self.call_tool(
            6,
            "search",
            {
                "query": "",
                "type": "experience",
                "experience_kind": "tasting",
                "related_id": product["id"],
                "relation_predicate": "involves",
                "occurred_after": "2026-07-01T00:00:00+02:00",
                "occurred_before": "2026-08-01T00:00:00+02:00",
                "min_rating": 5,
            },
        )
        self.assertFalse(found["result"]["isError"])  # type: ignore[index]
        results = found["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            [item["id"] for item in results["items"]],
            [experience["id"]],
        )
        self.assertEqual(results["items"][0]["attachment_count"], 1)

    def test_product_wishlist_experience_and_search(self) -> None:
        captured = self.call_tool(
            1,
            "capture",
            {
                "type": "product",
                "title": "MCP Test Rum",
                "text": "Diesen Rum möchte ich als Nächstes probieren.",
                "product_kind": "rum",
                "interest_status": "planned",
            },
        )
        result = captured["result"]["structuredContent"]  # type: ignore[index]

        planned = self.call_tool(
            2,
            "search",
            {
                "query": "",
                "type": "product",
                "product_kind": "rum",
                "interest_status": "planned",
            },
        )
        planned_results = planned["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            [item["id"] for item in planned_results["items"]],
            [result["id"]],
        )

        experienced = self.call_tool(
            3,
            "update",
            {
                "id": result["id"],
                "expected_revision": result["revision"],
                "record_experience": True,
                "experienced_at": "2026-07-31T21:00:00+02:00",
                "review": "Würzig und ausgewogen.",
                "rating": 5,
            },
        )
        experience_result = experienced["result"]["structuredContent"]  # type: ignore[index]
        self.assertIn("last_experienced_at", experience_result["changed"])

        photo = self.attachment_inbox / "mcp-rum.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\nmcp-rum-photo")
        attached = self.call_tool(
            4,
            "add_attachment",
            {
                "id": result["id"],
                "expected_revision": experience_result["revision"],
                "source_path": str(photo),
                "caption": "Etikett des Test-Rums",
                "experienced_at": "2026-07-31T21:00:00+02:00",
            },
        )
        self.assertFalse(attached["result"]["isError"])  # type: ignore[index]
        attachment_result = attached["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(
            attachment_result["attachment"]["media_type"], "image/png"
        )
        self.assertTrue(
            (self.root / attachment_result["attachment"]["path"]).is_file()
        )

        favorites = self.call_tool(
            5,
            "search",
            {
                "query": "",
                "type": "product",
                "product_kind": "rum",
                "experienced": True,
                "min_rating": 5,
            },
        )
        favorite_results = favorites["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(favorite_results["total"], 1)
        self.assertEqual(favorite_results["items"][0]["rating"], 5)

        wishlist_again = self.call_tool(
            6,
            "set_status",
            {
                "id": result["id"],
                "expected_revision": attachment_result["revision"],
                "interest": "wishlist",
            },
        )
        self.assertFalse(wishlist_again["result"]["isError"])  # type: ignore[index]

        selected = self.call_tool(7, "get_entry", {"id": result["id"]})
        selected_result = selected["result"]["structuredContent"]  # type: ignore[index]
        self.assertEqual(selected_result["attachment_count"], 1)
        self.assertEqual(len(selected_result["attachments"]), 1)

    def test_a_failed_tool_call_carries_a_stable_machine_readable_code(
        self,
    ) -> None:
        """The point of the typed error hierarchy, seen from the wire.

        A refusal used to reach the agent as one English sentence in
        `content[0].text`, so telling "retry after re-reading" apart from
        "this entry does not exist" meant matching prose that no test pinned
        down. Every refusal now also arrives as `structuredContent.error.code`.
        """

        entry = self.capture_note(1, "Fehlercode")

        stale = self.call_tool(
            2,
            "set_tags",
            {
                "id": entry["id"],
                "tags": ["beta"],
                "action": "add",
                "expected_revision": "sha256:" + "0" * 64,
            },
        )
        self.assertTrue(stale["result"]["isError"], stale)  # type: ignore[index]
        self.assertEqual(
            stale["result"]["structuredContent"]["error"]["code"],  # type: ignore[index]
            "revision_conflict",
        )

        missing = self.call_tool(
            3,
            "set_tags",
            {
                "id": "kn_" + "0" * 32,
                "tags": ["beta"],
                "action": "add",
                "expected_revision": entry["revision"],
            },
        )
        self.assertTrue(missing["result"]["isError"], missing)  # type: ignore[index]
        self.assertEqual(
            missing["result"]["structuredContent"]["error"]["code"],  # type: ignore[index]
            "not_found",
        )

        # Two different refusals, two different codes -- otherwise the field
        # would be decoration rather than something to branch on.
        self.assertNotEqual(
            stale["result"]["structuredContent"]["error"]["code"],  # type: ignore[index]
            missing["result"]["structuredContent"]["error"]["code"],  # type: ignore[index]
        )
        # The sentence is still there for a human reading the transcript.
        self.assertIn(
            "Revision conflict",
            stale["result"]["content"][0]["text"],  # type: ignore[index]
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from noetrail.migrations import CURRENT_SCHEMA_VERSION
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SECRET_SCRIPT = REPOSITORY_ROOT / "tools" / "check_secrets.py"


class KnowCliTest(unittest.TestCase):
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
            "    inverse: experienced_in\n"
            "  took_place_at:\n"
            "    inverse: hosted_experience\n",
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge" / "packs" / "knowledge-core",
            self.root / ".knowledge" / "packs" / "knowledge-core",
        )
        self.attachment_inbox = self.root / "matrix_files"
        self.attachment_inbox.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--root",
                str(self.root),
                "--attachment-inbox",
                str(self.attachment_inbox),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def rewrite_as_legacy_schema(
        self,
        path: Path,
        version: int | None,
        *,
        drop_fields: set[str] | None = None,
    ) -> str:
        lines = path.read_text(encoding="utf-8").splitlines()
        end = lines.index("---", 1)
        metadata: dict[str, object] = {}
        for line in lines[1:end]:
            key, raw_value = line.split(":", 1)
            metadata[key] = json.loads(raw_value.strip())
        attributes = metadata.pop("attributes", {})
        metadata.pop("type_version", None)
        if isinstance(attributes, dict):
            metadata.update(attributes)
        if version is None:
            metadata.pop("schema_version", None)
        else:
            metadata["schema_version"] = version
        for field in drop_fields or set():
            metadata.pop(field, None)
        rendered = ["---"]
        rendered.extend(
            f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
            for key, value in metadata.items()
        )
        rendered.extend(["---", *lines[end + 1 :]])
        content = "\n".join(rendered)
        if path.read_text(encoding="utf-8").endswith("\n"):
            content += "\n"
        path.write_text(content, encoding="utf-8")
        return content

    def read_entry(self, path: Path) -> tuple[dict[str, object], str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        end = lines.index("---", 1)
        metadata = {
            key: json.loads(raw_value.strip())
            for key, raw_value in (
                line.split(":", 1) for line in lines[1:end]
            )
        }
        return metadata, "\n".join(lines[end + 1 :]).strip()

    def test_capture_and_validate_memory(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "memory",
            "--title",
            "Sommer am See",
            "--text",
            "Ich glaube, es war im Sommer 2018.",
            "--occurred-at",
            "2018",
            "--occurred-precision",
            "approximate",
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertIn('type: "memory"', content)
        self.assertIn('"occurred_precision":"approximate"', content)
        self.assertIn("Ich glaube, es war im Sommer 2018.", content)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_duplicate_title_requires_explicit_override(self) -> None:
        first = self.run_cli(
            "capture", "--type", "thought", "--title", "Eine Idee", "--text", "A"
        )
        second = self.run_cli(
            "capture", "--type", "thought", "--title", "Eine Idee", "--text", "B"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)

    def test_search_returns_metadata_not_body(self) -> None:
        self.run_cli(
            "capture",
            "--type",
            "thought",
            "--title",
            "Gespräch statt Oberfläche",
            "--text",
            "Ein geheimer Gesprächspartner-Gedanke.",
            "--sensitivity",
            "sensitive",
        )
        searched = self.run_cli("search", "Gesprächspartner")
        self.assertEqual(searched.returncode, 0, searched.stderr)
        page = json.loads(searched.stdout)
        self.assertEqual(page["scope"], "active")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["returned"], 1)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(page["items"][0]["sensitivity"], "sensitive")
        self.assertNotIn("path", page["items"][0])
        self.assertNotIn("relations", page["items"][0])
        self.assertNotIn("geheimer", searched.stdout)

    def test_search_pagination_is_complete_compact_and_stable(self) -> None:
        for title in ("Charlie", "Alpha", "Bravo"):
            captured = self.run_cli(
                "capture",
                "--type",
                "thought",
                "--title",
                title,
                "--text",
                "Synthetic pagination marker.",
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)

        first = self.run_cli(
            "search",
            "pagination marker",
            "--sort",
            "title_asc",
            "--limit",
            "2",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_page = json.loads(first.stdout)
        self.assertEqual(first_page["sort"], "title_asc")
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(first_page["returned"], 2)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["next_offset"], 2)
        self.assertEqual(
            [item["title"] for item in first_page["items"]],
            ["Alpha", "Bravo"],
        )

        second = self.run_cli(
            "search",
            "pagination marker",
            "--sort",
            "title_asc",
            "--limit",
            "2",
            "--offset",
            "2",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_page = json.loads(second.stdout)
        self.assertEqual(second_page["total"], 3)
        self.assertEqual(second_page["returned"], 1)
        self.assertFalse(second_page["has_more"])
        self.assertIsNone(second_page["next_offset"])
        self.assertEqual(second_page["items"][0]["title"], "Charlie")

        too_large = self.run_cli(
            "search", "", "--limit", "51"
        )
        self.assertNotEqual(too_large.returncode, 0)
        self.assertIn("use --offset to continue", too_large.stderr)

    def test_retrieve_combines_search_and_bounded_full_reads(self) -> None:
        for title, body in (
            ("Alpha detail", "A" * 12),
            ("Bravo detail", "B" * 12),
        ):
            captured = self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                title,
                "--text",
                body,
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)

        retrieved = self.run_cli(
            "retrieve",
            "detail",
            "--limit",
            "2",
            "--max-body-chars",
            "15",
        )
        self.assertEqual(retrieved.returncode, 0, retrieved.stderr)
        result = json.loads(retrieved.stdout)
        self.assertEqual(result["mode"], "retrieve")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["body_chars_returned"], 15)
        self.assertEqual(sum(len(item["body"]) for item in result["items"]), 15)
        self.assertEqual(result["items"][0]["body_returned_chars"], 12)
        self.assertFalse(result["items"][0]["body_truncated"])
        self.assertEqual(result["items"][1]["body_returned_chars"], 3)
        self.assertTrue(result["items"][1]["body_truncated"])
        self.assertIn("relations", result["items"][0])
        self.assertIn("revision", result["items"][0])

        unbounded = self.run_cli("retrieve", "detail", "--limit", "11")
        self.assertNotEqual(unbounded.returncode, 0)
        self.assertIn("must not exceed 10", unbounded.stderr)

    def test_aliases_are_validated_searchable_and_revision_updated(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "product",
            "--title",
            "North Coast Twelve",
            "--alias",
            "NC12",
            "--alias",
            "North Coast 12",
            "--text",
            "Synthetic bottle note.",
            "--product-kind",
            "spirit",
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        created = json.loads(captured.stdout)

        found = json.loads(self.run_cli("search", "NC12").stdout)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["aliases"], ["NC12", "North Coast 12"])

        selected = json.loads(self.run_cli("review", created["id"]).stdout)
        self.assertEqual(selected["aliases"], ["NC12", "North Coast 12"])
        patch = self.write_json("aliases-update.json", {"aliases": ["NCT"]})
        updated = self.run_cli(
            "update",
            created["id"],
            "--patch-file",
            str(patch),
            "--expected-revision",
            selected["revision"],
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn("aliases", json.loads(updated.stdout)["changed"])
        self.assertEqual(
            json.loads(self.run_cli("search", "NCT").stdout)["total"],
            1,
        )

        duplicate_title = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Same name",
            "--alias",
            "same name",
            "--text",
            "Synthetic.",
        )
        self.assertNotEqual(duplicate_title.returncode, 0)
        self.assertIn("must differ from the title", duplicate_title.stderr)

    def test_inventory_is_complete_beyond_search_limit_without_content(self) -> None:
        destination = self.root / "vault" / "synthetic"
        destination.mkdir(parents=True)
        for index in range(205):
            entry_type = "product" if index == 204 else "thought"
            status = "unreviewed" if index < 5 else "active"
            attributes: dict[str, object] = {}
            if entry_type == "product":
                attributes = {
                    "product_kind": "rum",
                    "interest_status": "wishlist",
                }
            attachments = (
                [{"id": "one"}, {"id": "two"}] if index == 0 else []
            )
            relations = (
                [{"predicate": "related_to", "target": "kn_" + "f" * 32}]
                if index == 1
                else []
            )
            unresolved_relations = (
                [{"predicate": "related_to", "reference": "Later"}]
                if index == 2
                else []
            )
            metadata = {
                "id": f"kn_{index:032x}",
                "schema_version": 9,
                "type": entry_type,
                "type_version": 1,
                "title": f"Private title {index}",
                "created_at": "2026-08-02T00:00:00+00:00",
                "updated_at": "2026-08-02T00:00:00+00:00",
                "status": status,
                "sensitivity": "personal",
                "tags": [],
                "relations": relations,
                "unresolved_relations": unresolved_relations,
                "attachments": attachments,
                "attributes": attributes,
            }
            frontmatter = "\n".join(
                [
                    "---",
                    *(
                        f"{key}: "
                        + json.dumps(
                            value,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        for key, value in metadata.items()
                    ),
                    "---",
                    f"Private inventory marker {index}",
                    "",
                ]
            )
            (destination / f"entry-{index:03d}.md").write_text(
                frontmatter,
                encoding="utf-8",
            )

        inventoried = self.run_cli("inventory")
        self.assertEqual(inventoried.returncode, 0, inventoried.stderr)
        result = json.loads(inventoried.stdout)
        self.assertTrue(result["complete"])
        self.assertEqual(result["scope"], "active")
        self.assertEqual(result["entry_count"], 205)
        self.assertEqual(result["readable_entry_count"], 205)
        self.assertEqual(result["unreadable_entry_count"], 0)
        self.assertEqual(result["by_type"], {"product": 1, "thought": 204})
        self.assertEqual(result["by_status"], {"active": 200, "unreviewed": 5})
        self.assertEqual(result["facets"]["product_kind"], {"rum": 1})
        self.assertEqual(result["facets"]["interest_status"], {"wishlist": 1})
        self.assertEqual(result["entries_with_attachments"], 1)
        self.assertEqual(result["attachment_count"], 2)
        self.assertEqual(result["relation_count"], 1)
        self.assertEqual(result["unresolved_relation_count"], 1)
        self.assertNotIn("Private title", inventoried.stdout)
        self.assertNotIn("Private inventory marker", inventoried.stdout)

        (destination / "broken.md").write_text(
            "not frontmatter\n",
            encoding="utf-8",
        )
        incomplete = self.run_cli("inventory")
        self.assertEqual(incomplete.returncode, 0, incomplete.stderr)
        incomplete_result = json.loads(incomplete.stdout)
        self.assertFalse(incomplete_result["complete"])
        self.assertEqual(incomplete_result["entry_count"], 206)
        self.assertEqual(incomplete_result["readable_entry_count"], 205)
        self.assertEqual(incomplete_result["unreadable_entry_count"], 1)

    def test_relation_query_derives_inverse(self) -> None:
        project = self.run_cli(
            "capture",
            "--type",
            "project",
            "--title",
            "Knowledge Vault",
            "--text",
            "Das Projekt.",
        )
        project_id = json.loads(project.stdout)["id"]
        thought = self.run_cli(
            "capture",
            "--type",
            "thought",
            "--title",
            "Eine Projektidee",
            "--text",
            "Ein Gedanke zum Projekt.",
        )
        thought_id = json.loads(thought.stdout)["id"]

        related = self.run_cli("relate", thought_id, "related_to", project_id)
        self.assertEqual(related.returncode, 0, related.stderr)
        project_relations = self.run_cli("relations", project_id)
        project_result = json.loads(project_relations.stdout)
        self.assertEqual(project_result["incoming"][0]["predicate"], "related_to")
        self.assertEqual(project_result["incoming"][0]["source"], thought_id)

        thought_relations = self.run_cli("relations", thought_id)
        thought_result = json.loads(thought_relations.stdout)
        self.assertEqual(thought_result["outgoing"][0]["target"], project_id)

        removed = self.run_cli(
            "relate", thought_id, "related_to", project_id, "--remove"
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        after_removal = self.run_cli("relations", thought_id)
        self.assertEqual(json.loads(after_removal.stdout)["outgoing"], [])

    def test_bookmark_is_enriched_searchable_and_valid(self) -> None:
        payload = self.write_json(
            "bookmark.json",
            {
                "url": "https://example.com/rum?utm_source=newsletter#top",
                "title": "A Guide to Rum",
                "site_name": "Example",
                "authors": ["A. Writer"],
                "language": "en",
                "page_description": "A source description.",
                "summary": "Ein Überblick über karibischen Rum.",
                "tags": ["rum", "karibik"],
                "reading_status": "unread",
                "bookmark_kind": "article",
                "fetch_status": "complete",
                "relations": [],
            },
        )
        captured = self.run_cli(
            "bookmark", "--metadata-file", str(payload)
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        result = json.loads(captured.stdout)
        self.assertEqual(result["canonical_url"], "https://example.com/rum")

        path = self.root / result["created"]
        content = path.read_text(encoding="utf-8")
        self.assertIn('type: "bookmark"', content)
        self.assertIn('"domain":"example.com"', content)
        self.assertIn("Generated summary", content)

        searched = self.run_cli(
            "search", "karibischen", "--type", "bookmark", "--domain", "example.com"
        )
        self.assertEqual(searched.returncode, 0, searched.stderr)
        results = json.loads(searched.stdout)["items"]
        self.assertEqual(results[0]["reading_status"], "unread")
        self.assertEqual(results[0]["bookmark_kind"], "article")

        unread_articles = self.run_cli(
            "search",
            "",
            "--type",
            "bookmark",
            "--reading-status",
            "unread",
            "--bookmark-kind",
            "article",
        )
        self.assertEqual(unread_articles.returncode, 0, unread_articles.stderr)
        self.assertEqual(json.loads(unread_articles.stdout)["total"], 1)

        status = self.run_cli("status", result["id"], "--reading", "read")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_result = json.loads(status.stdout)
        self.assertIn("read_at", status_result["changed"])
        read_at = status_result["changed"]["read_at"]
        read_search = self.run_cli(
            "search",
            "",
            "--type",
            "bookmark",
            "--reading-status",
            "read",
            "--read-after",
            "2020-01-01T00:00:00+00:00",
            "--read-before",
            "2100-01-01T00:00:00+00:00",
        )
        self.assertEqual(read_search.returncode, 0, read_search.stderr)
        self.assertEqual(json.loads(read_search.stdout)["total"], 1)
        self.assertEqual(
            json.loads(read_search.stdout)["items"][0]["read_at"],
            read_at,
        )

        reference = self.run_cli(
            "status", result["id"], "--reading", "reference"
        )
        self.assertEqual(reference.returncode, 0, reference.stderr)
        reference_result = json.loads(reference.stdout)
        self.assertIsNone(reference_result["changed"]["read_at"])
        reference_search = self.run_cli(
            "search", "", "--type", "bookmark", "--reading-status", "reference"
        )
        self.assertEqual(json.loads(reference_search.stdout)["total"], 1)

        kind_patch = self.write_json(
            "bookmark-kind-update.json", {"bookmark_kind": "website"}
        )
        updated_kind = self.run_cli(
            "update",
            result["id"],
            "--patch-file",
            str(kind_patch),
            "--expected-revision",
            reference_result["revision"],
        )
        self.assertEqual(updated_kind.returncode, 0, updated_kind.stderr)
        self.assertIn("bookmark_kind", json.loads(updated_kind.stdout)["changed"])
        website_search = self.run_cli(
            "search", "", "--type", "bookmark", "--bookmark-kind", "website"
        )
        self.assertEqual(json.loads(website_search.stdout)["total"], 1)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_a_field_declared_searchable_is_reachable_through_search(
        self,
    ) -> None:
        """Bug: `searchable: true` did nothing for the built-in types.

        The haystack `search` matched against was a hand-written list of
        built-in field names, while the pack declared `searchable` per field.
        `bookmark.language` was declared searchable and appeared in no list,
        so nothing in the product could find a bookmark by its language --
        the declaration was inert. The haystack is now built from the same
        declaration the pack-defined types already used.
        """

        payload = self.write_json(
            "language.json",
            {
                "url": "https://example.com/quernstone",
                "title": "Ein Artikel",
                "language": "quernstone",
                "page_description": "Kurze Beschreibung.",
                "reading_status": "unread",
                "bookmark_kind": "article",
                "fetch_status": "complete",
            },
        )
        captured = self.run_cli("bookmark", "--metadata-file", str(payload))
        self.assertEqual(captured.returncode, 0, captured.stderr)
        stored = captured.stdout

        found = self.run_cli("search", "quernstone")
        self.assertEqual(found.returncode, 0, found.stderr)
        page = json.loads(found.stdout)
        self.assertEqual(page["total"], 1, stored)
        self.assertEqual(page["items"][0]["title"], "Ein Artikel")

        # Every field the pack marks searchable has to behave the same way,
        # otherwise the list is back, just hidden behind a loop.
        manifest = (
            self.root / ".knowledge" / "packs" / "knowledge-core" / "pack.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("      language:", manifest)
        described = self.run_cli("search", "kurze beschreibung")
        self.assertEqual(json.loads(described.stdout)["total"], 1)

    def test_bookmark_rejects_duplicate_canonical_url(self) -> None:
        first_payload = self.write_json(
            "first.json",
            {
                "url": "https://example.com/article?utm_campaign=test#section",
                "title": "First",
                "fetch_status": "complete",
            },
        )
        second_payload = self.write_json(
            "second.json",
            {
                "url": "https://example.com/article",
                "title": "Second",
                "fetch_status": "complete",
            },
        )
        first = self.run_cli("bookmark", "--metadata-file", str(first_payload))
        second = self.run_cli("bookmark", "--metadata-file", str(second_payload))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)

    def test_text_and_link_recipes_support_planning_and_cooking_history(
        self,
    ) -> None:
        text_recipe = self.run_cli(
            "capture",
            "--type",
            "recipe",
            "--title",
            "Pasta al Limone",
            "--text",
            "## Zutaten\n\n- Pasta\n- Zitrone\n\n## Zubereitung\n\nAlles vermengen.",
            "--interest-status",
            "planned",
            "--servings",
            "2 Portionen",
            "--prep-minutes",
            "10",
            "--cook-minutes",
            "15",
        )
        self.assertEqual(text_recipe.returncode, 0, text_recipe.stderr)
        text_result = json.loads(text_recipe.stdout)
        text_content = (self.root / text_result["created"]).read_text(
            encoding="utf-8"
        )
        self.assertIn('type: "recipe"', text_content)
        self.assertIn('"interest_status":"planned"', text_content)
        self.assertIn('"servings":"2 Portionen"', text_content)
        self.assertIn('"prep_minutes":10', text_content)
        self.assertIn("## Zutaten", text_content)

        linked_recipe = self.run_cli(
            "recipe",
            "https://example.com/pasta?utm_source=newsletter#ingredients",
            "--canonical-url",
            "https://example.com/pasta",
            "--title",
            "Pasta aus dem Netz",
            "--site-name",
            "Example Kitchen",
            "--source-description",
            "Source-provided recipe description.",
            "--text",
            "Möchte ich mit mehr Zitrone ausprobieren.",
            "--interest-status",
            "wishlist",
        )
        self.assertEqual(linked_recipe.returncode, 0, linked_recipe.stderr)
        linked_result = json.loads(linked_recipe.stdout)
        self.assertEqual(linked_result["source_url"], "https://example.com/pasta")
        linked_content = (self.root / linked_result["created"]).read_text(
            encoding="utf-8"
        )
        self.assertIn('"source_domain":"example.com"', linked_content)
        self.assertIn('"source_site_name":"Example Kitchen"', linked_content)
        self.assertIn("Möchte ich mit mehr Zitrone ausprobieren.", linked_content)

        duplicate = self.run_cli(
            "recipe",
            "https://example.com/pasta?utm_campaign=again",
            "--title",
            "Doppeltes Rezept",
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("source URL already exists", duplicate.stderr)

        planned = self.run_cli(
            "search",
            "",
            "--type",
            "recipe",
            "--interest-status",
            "planned",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(
            [item["id"] for item in json.loads(planned.stdout)["items"]],
            [text_result["id"]],
        )
        by_domain = self.run_cli(
            "search",
            "",
            "--type",
            "recipe",
            "--domain",
            "example.com",
        )
        self.assertEqual(by_domain.returncode, 0, by_domain.stderr)
        self.assertEqual(
            [item["id"] for item in json.loads(by_domain.stdout)["items"]],
            [linked_result["id"]],
        )

        cooked = self.run_cli(
            "capture",
            "--type",
            "experience",
            "--experience-kind",
            "cooking",
            "--occurred-at",
            "2026-08-01T18:30:00+02:00",
            "--rating",
            "5",
            "--relation",
            f"involves:{text_result['id']}",
            "--title",
            "Pasta al Limone gekocht",
            "--text",
            "Mehr Zitronenschale war genau richtig.",
        )
        self.assertEqual(cooked.returncode, 0, cooked.stderr)
        cooked_result = json.loads(cooked.stdout)
        history = self.run_cli(
            "search",
            "",
            "--type",
            "experience",
            "--experience-kind",
            "cooking",
            "--related-id",
            text_result["id"],
            "--relation-predicate",
            "involves",
            "--min-rating",
            "5",
        )
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(
            [item["id"] for item in json.loads(history.stdout)["items"]],
            [cooked_result["id"]],
        )

        completed = self.run_cli(
            "status",
            text_result["id"],
            "--interest",
            "none",
            "--expected-revision",
            text_result["revision"],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout)["changed"]["interest_status"],
            "none",
        )

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_places_products_wishlist_and_personal_experience(self) -> None:
        rum = self.run_cli(
            "capture",
            "--type",
            "product",
            "--product-kind",
            "rum",
            "--interest-status",
            "planned",
            "--title",
            "Example Reserve",
            "--text",
            "Diesen Rum möchte ich als Nächstes probieren.",
        )
        self.assertEqual(rum.returncode, 0, rum.stderr)
        rum_result = json.loads(rum.stdout)

        bar = self.run_cli(
            "capture",
            "--type",
            "place",
            "--place-kind",
            "bar",
            "--interest-status",
            "wishlist",
            "--title",
            "Example Bar",
            "--text",
            "Diese Bar möchte ich besuchen.",
        )
        self.assertEqual(bar.returncode, 0, bar.stderr)
        bar_result = json.loads(bar.stdout)

        next_rum = self.run_cli(
            "search",
            "",
            "--type",
            "product",
            "--product-kind",
            "rum",
            "--interest-status",
            "planned",
        )
        self.assertEqual(next_rum.returncode, 0, next_rum.stderr)
        next_results = json.loads(next_rum.stdout)["items"]
        self.assertEqual([item["id"] for item in next_results], [rum_result["id"]])

        premature_rating_patch = self.write_json(
            "premature-rating.json", {"rating": 5}
        )
        premature_rating = self.run_cli(
            "update",
            rum_result["id"],
            "--patch-file",
            str(premature_rating_patch),
            "--expected-revision",
            rum_result["revision"],
        )
        self.assertNotEqual(premature_rating.returncode, 0)
        self.assertIn(
            "rating requires a recorded experience", premature_rating.stderr
        )

        planned_bar = self.run_cli(
            "status",
            bar_result["id"],
            "--interest",
            "planned",
            "--expected-revision",
            bar_result["revision"],
        )
        self.assertEqual(planned_bar.returncode, 0, planned_bar.stderr)
        self.assertEqual(
            json.loads(planned_bar.stdout)["changed"]["interest_status"],
            "planned",
        )

        experience_patch = self.write_json(
            "rum-experience.json",
            {
                "record_experience": True,
                "experienced_at": "2026-07-31T20:30:00+02:00",
                "review": "Sehr fruchtig, aber nicht zu süß.",
                "rating": 4,
            },
        )
        experienced = self.run_cli(
            "update",
            rum_result["id"],
            "--patch-file",
            str(experience_patch),
            "--expected-revision",
            rum_result["revision"],
        )
        self.assertEqual(experienced.returncode, 0, experienced.stderr)
        experience_result = json.loads(experienced.stdout)
        self.assertLessEqual(
            {"last_experienced_at", "interest_status", "review", "rating"},
            set(experience_result["changed"]),
        )
        content = (self.root / rum_result["created"]).read_text(encoding="utf-8")
        self.assertIn('"interest_status":"none"', content)
        self.assertIn('"rating":4', content)
        self.assertIn(
            '"last_experienced_at":"2026-07-31T20:30:00+02:00"', content
        )
        self.assertIn("## Personal rating — 2026-07-31", content)
        self.assertIn("Sehr fruchtig, aber nicht zu süß.", content)

        tried_rum = self.run_cli(
            "search",
            "",
            "--type",
            "product",
            "--product-kind",
            "rum",
            "--experienced",
            "--min-rating",
            "4",
        )
        self.assertEqual(tried_rum.returncode, 0, tried_rum.stderr)
        tried_results = json.loads(tried_rum.stdout)["items"]
        self.assertEqual(len(tried_results), 1)
        self.assertEqual(tried_results[0]["rating"], 4)
        self.assertEqual(tried_results[0]["interest_status"], "none")

        revisit = self.run_cli(
            "status",
            rum_result["id"],
            "--interest",
            "wishlist",
            "--expected-revision",
            experience_result["revision"],
        )
        self.assertEqual(revisit.returncode, 0, revisit.stderr)
        revisit_result = json.loads(revisit.stdout)
        experienced_wishlist = self.run_cli(
            "search",
            "",
            "--type",
            "product",
            "--interest-status",
            "wishlist",
            "--experienced",
        )
        self.assertEqual(json.loads(experienced_wishlist.stdout)["total"], 1)

        second_experience_patch = self.write_json(
            "second-rum-experience.json",
            {
                "record_experience": True,
                "experienced_at": "2026-08-15T19:00:00+02:00",
                "review": "Beim zweiten Mal noch etwas würziger.",
            },
        )
        second_experience = self.run_cli(
            "update",
            rum_result["id"],
            "--patch-file",
            str(second_experience_patch),
            "--expected-revision",
            revisit_result["revision"],
        )
        self.assertEqual(
            second_experience.returncode, 0, second_experience.stderr
        )
        repeated_content = (self.root / rum_result["created"]).read_text(
            encoding="utf-8"
        )
        self.assertEqual(repeated_content.count("## Personal rating —"), 2)
        self.assertIn("Sehr fruchtig, aber nicht zu süß.", repeated_content)
        self.assertIn("Beim zweiten Mal noch etwas würziger.", repeated_content)
        self.assertIn('"interest_status":"none"', repeated_content)

        missing_kind = self.run_cli(
            "capture",
            "--type",
            "place",
            "--title",
            "Unvollständiger Ort",
            "--text",
            "Fehlender Typ.",
        )
        self.assertNotEqual(missing_kind.returncode, 0)
        self.assertIn("--place-kind is required", missing_kind.stderr)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_structured_experiences_are_repeatable_related_and_searchable(
        self,
    ) -> None:
        product = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "product",
                "--product-kind",
                "rum",
                "--interest-status",
                "planned",
                "--title",
                "Example Reserve",
                "--text",
                "Diesen Rum möchte ich probieren.",
            ).stdout
        )
        place = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "place",
                "--place-kind",
                "bar",
                "--title",
                "Example Bar",
                "--text",
                "Eine Bar.",
            ).stdout
        )

        first = self.run_cli(
            "capture",
            "--type",
            "experience",
            "--experience-kind",
            "tasting",
            "--occurred-at",
            "2026-07-31T21:30:00+02:00",
            "--occurred-precision",
            "datetime",
            "--rating",
            "4",
            "--relation",
            f"involves:{product['id']}",
            "--relation",
            f"took_place_at:{place['id']}",
            "--title",
            "Example Reserve in der Example Bar",
            "--text",
            "Sehr fruchtig, aber nicht zu süß.",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)

        repeated = self.run_cli(
            "capture",
            "--type",
            "experience",
            "--experience-kind",
            "tasting",
            "--occurred-at",
            "2026-08-01T20:00:00+02:00",
            "--relation",
            f"involves:{product['id']}",
            "--title",
            "Example Reserve in der Example Bar",
            "--text",
            "Beim zweiten Mal würziger.",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)

        photo = self.attachment_inbox / "experience.jpg"
        photo.write_bytes(b"\xff\xd8\xffstructured-experience-photo")
        attached = self.run_cli(
            "attach",
            first_result["id"],
            "experience.jpg",
            "--expected-revision",
            first_result["revision"],
            "--caption",
            "Flasche beim Tasting",
        )
        self.assertEqual(attached.returncode, 0, attached.stderr)

        found = self.run_cli(
            "search",
            "",
            "--type",
            "experience",
            "--experience-kind",
            "tasting",
            "--related-id",
            product["id"],
            "--relation-predicate",
            "involves",
            "--occurred-after",
            "2026-07-01T00:00:00+02:00",
            "--occurred-before",
            "2026-08-01T00:00:00+02:00",
            "--min-rating",
            "4",
        )
        self.assertEqual(found.returncode, 0, found.stderr)
        results = json.loads(found.stdout)["items"]
        self.assertEqual([item["id"] for item in results], [first_result["id"]])
        self.assertEqual(results[0]["experience_kind"], "tasting")
        self.assertEqual(results[0]["occurred_precision"], "datetime")
        self.assertEqual(results[0]["attachment_count"], 1)
        self.assertEqual(results[0]["relation_count"], 2)
        self.assertNotIn("relations", results[0])

        wrong_edge = self.run_cli(
            "search",
            "",
            "--type",
            "experience",
            "--related-id",
            product["id"],
            "--relation-predicate",
            "took_place_at",
        )
        self.assertEqual(wrong_edge.returncode, 0, wrong_edge.stderr)
        self.assertEqual(json.loads(wrong_edge.stdout)["items"], [])

        missing_kind = self.run_cli(
            "capture",
            "--type",
            "experience",
            "--title",
            "Unvollständiges Erlebnis",
            "--text",
            "Ohne Art.",
        )
        self.assertNotEqual(missing_kind.returncode, 0)
        self.assertIn("--experience-kind is required", missing_kind.stderr)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_legacy_experience_accepts_multiple_images_from_fixed_inbox(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "product",
            "--product-kind",
            "rum",
            "--title",
            "Photo Rum",
            "--text",
            "Diesen Rum habe ich probiert.",
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        result = json.loads(captured.stdout)
        experienced_at = "2026-07-31T21:30:00+02:00"
        experience_patch = self.write_json(
            "photo-rum-experience.json",
            {
                "record_experience": True,
                "experienced_at": experienced_at,
                "review": "Fruchtig und weich.",
                "rating": 4,
            },
        )
        experienced = self.run_cli(
            "update",
            result["id"],
            "--patch-file",
            str(experience_patch),
            "--expected-revision",
            result["revision"],
        )
        self.assertEqual(experienced.returncode, 0, experienced.stderr)
        experience_result = json.loads(experienced.stdout)

        first_bytes = b"\x89PNG\r\n\x1a\nsynthetic-rum-photo"
        first_source = (
            self.attachment_inbox
            / "0123456789abcdef0123456789abcdef_rum-label.png"
        )
        first_source.write_bytes(first_bytes)
        mismatched = self.run_cli(
            "attach",
            result["id"],
            str(first_source),
            "--expected-revision",
            experience_result["revision"],
            "--expected-source-sha256",
            "0" * 64,
        )
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertIn("changed after it was listed", mismatched.stderr)
        first = self.run_cli(
            "attach",
            result["id"],
            str(first_source),
            "--expected-revision",
            experience_result["revision"],
            "--expected-source-sha256",
            hashlib.sha256(first_bytes).hexdigest(),
            "--caption",
            "Flasche und Etikett",
            "--experienced-at",
            experienced_at,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)
        first_attachment = first_result["attachment"]
        self.assertEqual(first_attachment["original_name"], "rum-label.png")
        self.assertEqual(first_attachment["media_type"], "image/png")
        self.assertEqual(
            first_attachment["sha256"], hashlib.sha256(first_bytes).hexdigest()
        )
        self.assertEqual(first_attachment["experienced_at"], experienced_at)
        self.assertEqual(first_attachment["caption"], "Flasche und Etikett")
        stored = self.root / first_attachment["path"]
        self.assertEqual(stored.read_bytes(), first_bytes)

        second_source = self.attachment_inbox / "glas.jpg"
        second_source.write_bytes(b"\xff\xd8\xffsynthetic-glass-photo")
        second = self.run_cli(
            "attach",
            result["id"],
            "glas.jpg",
            "--expected-revision",
            first_result["revision"],
            "--experienced-at",
            experienced_at,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)

        duplicate = self.run_cli(
            "attach",
            result["id"],
            str(first_source),
            "--expected-revision",
            second_result["revision"],
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("already attached", duplicate.stderr)

        selected = self.run_cli("review", result["id"])
        selected_result = json.loads(selected.stdout)
        self.assertEqual(selected_result["attachment_count"], 2)
        self.assertEqual(len(selected_result["attachments"]), 2)
        searched = self.run_cli("search", "", "--type", "product")
        self.assertEqual(
            json.loads(searched.stdout)["items"][0]["attachment_count"],
            2,
        )

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_attach_rejects_stale_unsafe_and_unsupported_sources(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Attachment safety",
                "--text",
                "Synthetic test.",
            ).stdout
        )
        outside = self.root / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
        rejected_outside = self.run_cli(
            "attach",
            captured["id"],
            str(outside),
            "--expected-revision",
            captured["revision"],
        )
        self.assertNotEqual(rejected_outside.returncode, 0)
        self.assertIn("outside the configured inbox", rejected_outside.stderr)
        self.assertNotIn(str(outside), rejected_outside.stderr)

        unsupported = self.attachment_inbox / "payload.svg"
        unsupported.write_text("<svg></svg>", encoding="utf-8")
        rejected_type = self.run_cli(
            "attach",
            captured["id"],
            str(unsupported),
            "--expected-revision",
            captured["revision"],
        )
        self.assertNotEqual(rejected_type.returncode, 0)
        self.assertIn("not a supported", rejected_type.stderr)

        symlink_target = self.attachment_inbox / "target.png"
        symlink_target.write_bytes(b"\x89PNG\r\n\x1a\nsymlink-target")
        symlink_source = self.attachment_inbox / "linked.png"
        symlink_source.symlink_to(symlink_target)
        rejected_symlink = self.run_cli(
            "attach",
            captured["id"],
            str(symlink_source),
            "--expected-revision",
            captured["revision"],
        )
        self.assertNotEqual(rejected_symlink.returncode, 0)
        self.assertIn("symbolic link", rejected_symlink.stderr)

        oversized = self.attachment_inbox / "oversized.png"
        with oversized.open("wb") as handle:
            handle.truncate(20 * 1024 * 1024 + 1)
        rejected_size = self.run_cli(
            "attach",
            captured["id"],
            str(oversized),
            "--expected-revision",
            captured["revision"],
        )
        self.assertNotEqual(rejected_size.returncode, 0)
        self.assertIn("20971520-byte limit", rejected_size.stderr)

        first = self.attachment_inbox / "first.png"
        first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
        attached = self.run_cli(
            "attach",
            captured["id"],
            str(first),
            "--expected-revision",
            captured["revision"],
        )
        self.assertEqual(attached.returncode, 0, attached.stderr)
        second = self.attachment_inbox / "second.png"
        second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
        stale = self.run_cli(
            "attach",
            captured["id"],
            str(second),
            "--expected-revision",
            captured["revision"],
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("Revision conflict", stale.stderr)
        self.assertEqual(
            len(list((self.root / "vault" / "attachments").rglob("*.png"))),
            1,
        )

    def test_attachment_validation_detects_blob_tampering(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Tamper test",
                "--text",
                "Synthetic test.",
            ).stdout
        )
        source = self.attachment_inbox / "photo.png"
        original = b"\x89PNG\r\n\x1a\noriginal-photo"
        source.write_bytes(original)
        attached = json.loads(
            self.run_cli(
                "attach",
                captured["id"],
                str(source),
                "--expected-revision",
                captured["revision"],
            ).stdout
        )
        stored = self.root / attached["attachment"]["path"]
        stored.write_bytes(b"\x89PNG\r\n\x1a\ntampered-photo")

        validated = self.run_cli("validate")
        self.assertNotEqual(validated.returncode, 0)
        self.assertIn("file hash does not match sha256", validated.stdout)

    def test_attach_rejects_symlinked_storage_directory(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Storage safety",
                "--text",
                "Synthetic test.",
            ).stdout
        )
        outside_storage = self.root / "outside-storage"
        outside_storage.mkdir()
        (self.root / "vault" / "attachments").symlink_to(
            outside_storage, target_is_directory=True
        )
        source = self.attachment_inbox / "photo.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nphoto")

        rejected = self.run_cli(
            "attach",
            captured["id"],
            str(source),
            "--expected-revision",
            captured["revision"],
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("storage root is not a regular directory", rejected.stderr)
        self.assertEqual(list(outside_storage.iterdir()), [])

    def test_update_appends_without_replacing_original_body(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "thought",
            "--title",
            "Erster Titel",
            "--text",
            "Ursprünglicher Gedanke.",
        )
        result = json.loads(captured.stdout)
        patch = self.write_json(
            "update.json",
            {
                "title": "Verbesserter Titel",
                "append": "## Ergänzung\n\nEin neuer Aspekt.",
                "sensitivity": "sensitive",
            },
        )
        updated = self.run_cli(
            "update", result["id"], "--patch-file", str(patch)
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        content = (self.root / result["created"]).read_text(encoding="utf-8")
        self.assertIn('title: "Verbesserter Titel"', content)
        self.assertIn('sensitivity: "sensitive"', content)
        self.assertIn("Ursprünglicher Gedanke.", content)
        self.assertIn("Ein neuer Aspekt.", content)

    def test_stale_revision_cannot_overwrite_a_newer_update(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Parallele Bearbeitung",
            "--text",
            "Ausgangsstand.",
        )
        created = json.loads(captured.stdout)
        initial_revision = created["revision"]
        first_patch = self.write_json(
            "first-update.json",
            {"append": "Erste bestätigte Ergänzung."},
        )
        first = self.run_cli(
            "update",
            created["id"],
            "--patch-file",
            str(first_patch),
            "--expected-revision",
            initial_revision,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        current = json.loads(first.stdout)
        self.assertNotEqual(current["revision"], initial_revision)

        stale_patch = self.write_json(
            "stale-update.json",
            {"append": "Diese veraltete Ergänzung darf nicht hinein."},
        )
        stale = self.run_cli(
            "update",
            created["id"],
            "--patch-file",
            str(stale_patch),
            "--expected-revision",
            initial_revision,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("Revision conflict", stale.stderr)

        path = self.root / created["created"]
        content = path.read_text(encoding="utf-8")
        self.assertIn("Erste bestätigte Ergänzung.", content)
        self.assertNotIn("veraltete Ergänzung", content)

    def test_update_requires_confirmation_for_body_replacement(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Zu schützen",
            "--text",
            "Original.",
        )
        result = json.loads(captured.stdout)
        patch = self.write_json("replace.json", {"replace_body": "Neu."})
        rejected = self.run_cli(
            "update", result["id"], "--patch-file", str(patch)
        )
        self.assertNotEqual(rejected.returncode, 0)
        content = (self.root / result["created"]).read_text(encoding="utf-8")
        self.assertIn("Original.", content)
        self.assertNotIn("Neu.", content)

    def test_tags_can_be_added_and_removed(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Tag-Test",
            "--text",
            "Inhalt.",
        )
        result = json.loads(captured.stdout)
        added = self.run_cli("tag", result["id"], "Rum", "Karibik")
        self.assertEqual(added.returncode, 0, added.stderr)
        # Removal matches case-insensitively ...
        removed = self.run_cli("tag", result["id"], "rum", "--remove")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        # ... but reports the spelling that was actually stored.
        self.assertEqual(json.loads(removed.stdout)["tags"], ["Rum"])
        content = (self.root / result["created"]).read_text(encoding="utf-8")
        # ... and the surviving tag keeps the author's capitalisation.
        self.assertIn('tags: ["Karibik"]', content)

    def test_tagging_preserves_the_spelling_of_untouched_tags(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Tag-Schreibweise",
            "--text",
            "Inhalt.",
            "--tag",
            "Straße",
            "--tag",
            "Machine Learning",
        )
        result = json.loads(captured.stdout)
        added = self.run_cli("tag", result["id"], "physics")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)["tags"], ["physics"])
        content = (self.root / result["created"]).read_text(encoding="utf-8")
        # `casefold` maps "ß" to "ss"; folding on write made that irreversible
        # and silently rewrote every other tag on the entry.
        self.assertIn('tags: ["Machine Learning","Straße","physics"]', content)
        self.assertNotIn("strasse", content)
        self.assertNotIn("machine learning", content)

    def test_tagging_is_case_insensitive_without_creating_duplicates(
        self,
    ) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Tag-Duplikate",
            "--text",
            "Inhalt.",
            "--tag",
            "Rum",
        )
        result = json.loads(captured.stdout)
        rejected = self.run_cli("tag", result["id"], "RUM")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("already exist", rejected.stderr)
        content = (self.root / result["created"]).read_text(encoding="utf-8")
        self.assertIn('tags: ["Rum"]', content)

    def test_legacy_entry_migrates_dry_run_first_and_idempotently(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Legacy",
            "--text",
            "Der Inhalt muss unverändert bleiben.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        legacy = self.rewrite_as_legacy_schema(path, None)

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        dry_result = json.loads(dry_run.stdout)
        self.assertEqual(dry_result["migrated"][0]["from"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), legacy)

        applied = self.run_cli("migrate", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        migrated = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", migrated)
        self.assertIn("Der Inhalt muss unverändert bleiben.", migrated)
        original_updated_at = next(
            line for line in legacy.splitlines() if line.startswith("updated_at:")
        )
        self.assertIn(original_updated_at, migrated)

        second_run = self.run_cli("migrate", "--apply")
        self.assertEqual(json.loads(second_run.stdout)["migrated"], [])
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_migrate_previews_by_default_and_only_writes_with_apply(
        self,
    ) -> None:
        """`migrate` must not rewrite the vault unless asked to.

        Every other destructive command previews by default and writes under an
        explicit flag. `migrate` inverted that: a bare `noetrail migrate`
        rewrote every entry in the vault, with no backup and no way back.
        """
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Vorschau",
            "--text",
            "Unverändert.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        legacy = self.rewrite_as_legacy_schema(path, None)

        preview = self.run_cli("migrate")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        preview_result = json.loads(preview.stdout)
        self.assertTrue(preview_result["dry_run"])
        self.assertEqual(len(preview_result["migrated"]), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), legacy)

        # `--dry-run` stays accepted so existing runbooks keep working.
        legacy_flag = self.run_cli("migrate", "--dry-run")
        self.assertEqual(legacy_flag.returncode, 0, legacy_flag.stderr)
        self.assertTrue(json.loads(legacy_flag.stdout)["dry_run"])
        self.assertEqual(path.read_text(encoding="utf-8"), legacy)

        applied = self.run_cli("migrate", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse(json.loads(applied.stdout)["dry_run"])
        self.assertIn(
            f"schema_version: {CURRENT_SCHEMA_VERSION}",
            path.read_text(encoding="utf-8"),
        )

    def test_schema_v1_migrates_to_current_without_semantic_changes(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Schema eins",
            "--text",
            "Dieser Text bleibt exakt erhalten.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        version_one = self.rewrite_as_legacy_schema(path, 1)
        original_updated_at = next(
            line
            for line in version_one.splitlines()
            if line.startswith("updated_at:")
        )

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        planned = json.loads(dry_run.stdout)["migrated"][0]
        self.assertEqual(planned["from"], 1)
        self.assertEqual(planned["to"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            planned["steps"],
            [
                "1->2:add-trash-lifecycle",
                "2->3:add-review-workflow",
                "3->4:add-bookmark-reading-metadata",
                "4->5:add-places-products-experiences",
                "5->6:add-attachment-references",
                "6->7:add-structured-experiences",
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), version_one)

        applied = self.run_cli("migrate", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        migrated = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", migrated)
        self.assertIn("Dieser Text bleibt exakt erhalten.", migrated)
        self.assertIn(original_updated_at, migrated)

    def test_schema_v2_migrates_without_marking_existing_entries(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Schema zwei",
                "--text",
                "Bestehende Entscheidungen bleiben bestehen.",
            ).stdout
        )
        path = self.root / captured["created"]
        self.rewrite_as_legacy_schema(path, 2)

        migrated = self.run_cli("migrate", "--apply")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        plan = json.loads(migrated.stdout)["migrated"][0]
        self.assertEqual(
            plan["steps"],
            [
                "2->3:add-review-workflow",
                "3->4:add-bookmark-reading-metadata",
                "4->5:add-places-products-experiences",
                "5->6:add-attachment-references",
                "6->7:add-structured-experiences",
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertNotIn("reviewed_at:", content)
        self.assertIn('status: "active"', content)

    def test_schema_v3_bookmark_gets_unknown_kind_without_read_date(self) -> None:
        payload = self.write_json(
            "legacy-bookmark.json",
            {
                "url": "https://example.com/legacy-article",
                "title": "Legacy article",
                "reading_status": "read",
            },
        )
        captured = json.loads(
            self.run_cli("bookmark", "--metadata-file", str(payload)).stdout
        )
        path = self.root / captured["created"]
        version_three = self.rewrite_as_legacy_schema(
            path,
            3,
            drop_fields={"bookmark_kind", "read_at"},
        )

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plan = json.loads(dry_run.stdout)["migrated"][0]
        self.assertEqual(
            plan["steps"],
            [
                "3->4:add-bookmark-reading-metadata",
                "4->5:add-places-products-experiences",
                "5->6:add-attachment-references",
                "6->7:add-structured-experiences",
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), version_three)

        migrated = self.run_cli("migrate", "--apply")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertIn('"bookmark_kind":"unknown"', content)
        self.assertNotIn("read_at:", content)
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_schema_v4_migrates_without_changing_existing_entry_data(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Schema vier",
                "--text",
                "Bestehender Inhalt bleibt gleich.",
            ).stdout
        )
        path = self.root / captured["created"]
        version_four = self.rewrite_as_legacy_schema(path, 4)

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plan = json.loads(dry_run.stdout)["migrated"][0]
        self.assertEqual(
            plan["steps"],
            [
                "4->5:add-places-products-experiences",
                "5->6:add-attachment-references",
                "6->7:add-structured-experiences",
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), version_four)

        migrated = self.run_cli("migrate", "--apply")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertIn("Bestehender Inhalt bleibt gleich.", content)

    def test_schema_v5_migrates_without_inventing_attachments(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "product",
                "--product-kind",
                "rum",
                "--title",
                "Schema fünf Rum",
                "--text",
                "Bestehender Rum ohne Fotos.",
            ).stdout
        )
        path = self.root / captured["created"]
        version_five = self.rewrite_as_legacy_schema(path, 5)

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plan = json.loads(dry_run.stdout)["migrated"][0]
        self.assertEqual(
            plan["steps"],
            [
                "5->6:add-attachment-references",
                "6->7:add-structured-experiences",
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), version_five)

        migrated = self.run_cli("migrate", "--apply")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertNotIn("attachments:", content)
        self.assertIn("Bestehender Rum ohne Fotos.", content)

    def test_schema_v6_migrates_without_inventing_experiences(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "product",
                "--product-kind",
                "rum",
                "--title",
                "Schema sechs Rum",
                "--text",
                "Bestehende Bewertung bleibt am Produkt.",
            ).stdout
        )
        path = self.root / captured["created"]
        version_six = self.rewrite_as_legacy_schema(path, 6)

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plan = json.loads(dry_run.stdout)["migrated"][0]
        self.assertEqual(
            plan["steps"],
            [
                "6->7:add-structured-experiences",
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), version_six)

        migrated = self.run_cli("migrate", "--apply")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertNotIn('type: "experience"', content)
        self.assertFalse((self.root / "vault" / "experiences").exists())

    def test_schema_v7_migrates_without_inventing_recipes_or_cooking(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Schema sieben Notiz",
                "--text",
                "Diese Notiz bleibt eine Notiz.",
            ).stdout
        )
        path = self.root / captured["created"]
        version_seven = self.rewrite_as_legacy_schema(path, 7)

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plan = json.loads(dry_run.stdout)["migrated"][0]
        self.assertEqual(
            plan["steps"],
            [
                "7->8:add-recipes-and-cooking",
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), version_seven)

        migrated = self.run_cli("migrate", "--apply")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {CURRENT_SCHEMA_VERSION}", content)
        self.assertNotIn('type: "recipe"', content)
        self.assertNotIn('experience_kind: "cooking"', content)
        self.assertFalse((self.root / "vault" / "recipes").exists())

    def test_schema_v8_migration_describes_move_and_preserves_core(self) -> None:
        project = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "project",
                "--title",
                "Migration target",
                "--text",
                "Synthetic relation target.",
            ).stdout
        )
        product = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "product",
                "--product-kind",
                "rum",
                "--interest-status",
                "planned",
                "--title",
                "Migration product",
                "--text",
                "The body must remain unchanged.",
                "--relation",
                f"related_to:{project['id']}",
            ).stdout
        )
        path = self.root / product["created"]
        legacy = self.rewrite_as_legacy_schema(path, 8)
        before_metadata, before_body = self.read_entry(path)

        dry_run = self.run_cli("migrate", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plans = json.loads(dry_run.stdout)["migrated"]
        selected = next(item for item in plans if item["path"] == product["created"])
        self.assertEqual(
            selected["steps"],
            [
                "8->9:move-builtins-to-attributes",
                "9->10:add-field-provenance",
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(
            selected["changes"],
            {
                "moved_to_attributes": ["interest_status", "product_kind"],
                "added_top_level": ["attributes", "type_version"],
                "removed_top_level": [],
                "body_changed": False,
            },
        )
        self.assertEqual(path.read_text(encoding="utf-8"), legacy)

        applied = self.run_cli("migrate", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        after_metadata, after_body = self.read_entry(path)
        for field in {
            "id",
            "type",
            "title",
            "created_at",
            "updated_at",
            "status",
            "sensitivity",
            "tags",
            "relations",
            "origin",
        }:
            self.assertEqual(after_metadata[field], before_metadata[field])
        self.assertEqual(after_body, before_body)
        self.assertEqual(
            after_metadata["attributes"],
            {"product_kind": "rum", "interest_status": "planned"},
        )
        self.assertEqual(after_metadata["type_version"], 1)

    def test_schema_v8_entry_remains_editable_before_migration(self) -> None:
        product = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "product",
                "--product-kind",
                "drink",
                "--title",
                "Compatibility product",
                "--text",
                "Still stored in the legacy layout.",
            ).stdout
        )
        path = self.root / product["created"]
        legacy = self.rewrite_as_legacy_schema(path, 8)
        revision = f"sha256:{hashlib.sha256(legacy.encode()).hexdigest()}"

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)
        updated = self.run_cli(
            "status",
            product["id"],
            "--interest",
            "planned",
            "--expected-revision",
            revision,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn("schema_version: 8", content)
        self.assertIn('interest_status: "planned"', content)
        self.assertNotIn("attributes:", content)

    def test_review_lists_expected_queue_without_batch_body_content(self) -> None:
        thought = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "thought",
                "--title",
                "Schneller Gedanke",
                "--text",
                "Sehr persönlicher Queue-Inhalt.",
            ).stdout
        )
        thought_path = self.root / thought["created"]
        self.assertIn(
            'status: "unreviewed"',
            thought_path.read_text(encoding="utf-8"),
        )

        imported = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Importierte Notiz",
                "--text",
                "Importierter geheimer Inhalt.",
            ).stdout
        )
        marked = self.run_cli(
            "status", imported["id"], "--lifecycle", "unreviewed"
        )
        self.assertEqual(marked.returncode, 0, marked.stderr)

        without_note_payload = self.write_json(
            "without-note.json",
            {
                "url": "https://example.com/without-note",
                "title": "Bookmark ohne Notiz",
                "summary": "Nur automatisch zusammengefasst.",
            },
        )
        without_note = json.loads(
            self.run_cli(
                "bookmark", "--metadata-file", str(without_note_payload)
            ).stdout
        )
        with_note_payload = self.write_json(
            "with-note.json",
            {
                "url": "https://example.com/with-note",
                "title": "Bookmark mit Notiz",
                "note": "Dafür habe ich ihn gespeichert.",
            },
        )
        with_note = json.loads(
            self.run_cli(
                "bookmark", "--metadata-file", str(with_note_payload)
            ).stdout
        )

        review = self.run_cli("review")
        self.assertEqual(review.returncode, 0, review.stderr)
        queue = json.loads(review.stdout)
        by_id = {item["id"]: item for item in queue["items"]}
        self.assertEqual(queue["count"], 3)
        self.assertIn(thought["id"], by_id)
        self.assertIn(imported["id"], by_id)
        self.assertIn(without_note["id"], by_id)
        self.assertNotIn(with_note["id"], by_id)
        self.assertEqual(by_id[thought["id"]]["reasons"], ["unreviewed"])
        self.assertEqual(
            by_id[without_note["id"]]["reasons"],
            ["bookmark_missing_personal_note"],
        )
        self.assertNotIn("Sehr persönlicher Queue-Inhalt", review.stdout)
        self.assertNotIn("Importierter geheimer Inhalt", review.stdout)

        detail = self.run_cli("review", thought["id"])
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertIn("Sehr persönlicher Queue-Inhalt", detail.stdout)

    def test_review_completion_activates_thought_and_acks_bookmark(self) -> None:
        thought = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "thought",
                "--title",
                "Zu prüfender Gedanke",
                "--text",
                "Noch roh.",
            ).stdout
        )
        bookmark_payload = self.write_json(
            "review-bookmark.json",
            {
                "url": "https://example.com/review",
                "title": "Zu prüfender Bookmark",
            },
        )
        bookmark = json.loads(
            self.run_cli(
                "bookmark", "--metadata-file", str(bookmark_payload)
            ).stdout
        )

        thought_review = self.run_cli(
            "review", thought["id"], "--complete"
        )
        self.assertEqual(thought_review.returncode, 0, thought_review.stderr)
        thought_content = (self.root / thought["created"]).read_text(
            encoding="utf-8"
        )
        self.assertIn('status: "active"', thought_content)
        self.assertIn("reviewed_at:", thought_content)

        bookmark_review = self.run_cli(
            "review", bookmark["id"], "--complete"
        )
        self.assertEqual(bookmark_review.returncode, 0, bookmark_review.stderr)
        remaining_ids = {
            item["id"]
            for item in json.loads(self.run_cli("review").stdout)["items"]
        }
        self.assertNotIn(thought["id"], remaining_ids)
        self.assertNotIn(bookmark["id"], remaining_ids)

    def test_unresolved_relation_must_be_resolved_before_review_completion(
        self,
    ) -> None:
        project = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "project",
                "--title",
                "Projekt Atlas",
                "--text",
                "Das Ziel.",
            ).stdout
        )
        thought = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "thought",
                "--title",
                "Gedanke zu Atlas",
                "--text",
                "Bezug noch nicht geklärt.",
            ).stdout
        )
        pending = self.run_cli(
            "relate",
            thought["id"],
            "related_to",
            "Projekt Atlas",
            "--unresolved",
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        review_item = next(
            item
            for item in json.loads(self.run_cli("review").stdout)["items"]
            if item["id"] == thought["id"]
        )
        self.assertIn("unresolved_relations", review_item["reasons"])
        self.assertEqual(
            review_item["unresolved_relations"][0]["reference"],
            "Projekt Atlas",
        )

        refused = self.run_cli("review", thought["id"], "--complete")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Resolve or remove", refused.stderr)

        resolved = self.run_cli(
            "relate",
            thought["id"],
            "related_to",
            project["id"],
            "--resolve-reference",
            "Projekt Atlas",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["action"], "resolved")
        completed = self.run_cli("review", thought["id"], "--complete")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        relations = json.loads(
            self.run_cli("relations", thought["id"]).stdout
        )
        self.assertEqual(relations["outgoing"][0]["target"], project["id"])
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_trash_is_excluded_from_search_and_can_be_restored(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "thought",
            "--title",
            "Wiederkehrender Gedanke",
            "--text",
            "Ein unverwechselbarer Papierkorb-Inhalt.",
        )
        result = json.loads(captured.stdout)
        original_path = self.root / result["created"]
        archived = self.run_cli(
            "status", result["id"], "--lifecycle", "archived"
        )
        self.assertEqual(archived.returncode, 0, archived.stderr)

        trashed = self.run_cli(
            "trash", result["id"], "--reason", "Vorläufig aussortiert"
        )
        self.assertEqual(trashed.returncode, 0, trashed.stderr)
        trash_result = json.loads(trashed.stdout)
        trash_path = self.root / trash_result["trashed"]
        self.assertFalse(original_path.exists())
        self.assertTrue(trash_path.exists())
        trash_content = trash_path.read_text(encoding="utf-8")
        self.assertIn('status: "trashed"', trash_content)
        self.assertIn('previous_status: "archived"', trash_content)
        self.assertIn('deleted_from: "vault/', trash_content)
        self.assertIn('deletion_reason: "Vorläufig aussortiert"', trash_content)

        searched = self.run_cli("search", "Papierkorb-Inhalt")
        self.assertEqual(json.loads(searched.stdout)["items"], [])
        listed = self.run_cli("trash", "list")
        listed_entries = json.loads(listed.stdout)
        self.assertEqual(listed_entries[0]["id"], result["id"])
        self.assertNotIn("Papierkorb-Inhalt", listed.stdout)
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

        restored = self.run_cli("restore", result["id"])
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertTrue(original_path.exists())
        self.assertFalse(trash_path.exists())
        restored_content = original_path.read_text(encoding="utf-8")
        self.assertIn('status: "archived"', restored_content)
        self.assertNotIn("deleted_at:", restored_content)
        self.assertNotIn("deleted_from:", restored_content)
        self.assertEqual(
            len(
                json.loads(
                    self.run_cli("search", "Papierkorb-Inhalt").stdout
                )["items"]
            ),
            1,
        )

    def test_active_relation_to_trashed_target_is_a_warning(self) -> None:
        target = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "project",
                "--title",
                "Temporäres Projekt",
                "--text",
                "Ziel.",
            ).stdout
        )
        source = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "thought",
                "--title",
                "Verknüpfter Gedanke",
                "--text",
                "Quelle.",
            ).stdout
        )
        related = self.run_cli(
            "relate", source["id"], "related_to", target["id"]
        )
        self.assertEqual(related.returncode, 0, related.stderr)
        trashed = self.run_cli("trash", target["id"])
        self.assertEqual(trashed.returncode, 0, trashed.stderr)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)
        self.assertIn("WARN", validated.stdout)
        self.assertIn("targets trashed entry", validated.stdout)
        relations = json.loads(self.run_cli("relations", source["id"]).stdout)
        self.assertEqual(relations["outgoing"][0]["target_status"], "trashed")

    def test_purge_is_dry_run_by_default_and_requires_apply(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Später endgültig löschen",
                "--text",
                "Nur für den Test.",
            ).stdout
        )
        trash_result = json.loads(
            self.run_cli("trash", captured["id"]).stdout
        )
        trash_path = self.root / trash_result["trashed"]

        preview = self.run_cli("purge", "--id", captured["id"])
        self.assertEqual(preview.returncode, 0, preview.stderr)
        preview_result = json.loads(preview.stdout)
        self.assertTrue(preview_result["dry_run"])
        self.assertEqual(preview_result["candidates"][0]["id"], captured["id"])
        self.assertTrue(trash_path.exists())

        applied = self.run_cli("purge", "--id", captured["id"], "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        applied_result = json.loads(applied.stdout)
        self.assertFalse(applied_result["dry_run"])
        self.assertEqual(applied_result["purged"][0]["id"], captured["id"])
        self.assertTrue(applied_result["backup_copies_unchanged"])
        self.assertFalse(trash_path.exists())
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_restore_refuses_to_overwrite_original_path(self) -> None:
        captured = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                "Pfadkonflikt",
                "--text",
                "Original.",
            ).stdout
        )
        original_path = self.root / captured["created"]
        trash_result = json.loads(
            self.run_cli("trash", captured["id"]).stdout
        )
        trash_path = self.root / trash_result["trashed"]
        original_path.write_text("occupied", encoding="utf-8")

        restored = self.run_cli("restore", captured["id"])
        self.assertNotEqual(restored.returncode, 0)
        self.assertIn("already exists", restored.stderr)
        self.assertEqual(original_path.read_text(encoding="utf-8"), "occupied")
        self.assertTrue(trash_path.exists())

    def test_future_schema_version_is_refused_without_changes(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Aus der Zukunft",
            "--text",
            "Nicht erraten.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        future = path.read_text(encoding="utf-8").replace(
            f"schema_version: {CURRENT_SCHEMA_VERSION}", "schema_version: 99"
        )
        path.write_text(future, encoding="utf-8")

        migration = self.run_cli("migrate", "--apply")
        self.assertNotEqual(migration.returncode, 0)
        self.assertIn("no files were changed", migration.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), future)


    def test_migration_fills_pack_defaults_for_fields_added_later(
        self,
    ) -> None:
        """A field that gained a default after the entry was written.

        `migrate_8_to_9` only moved fields. An entry written before
        `interest_status` existed came out of the migration without it,
        reported as a success, and failed `validate` immediately afterwards.
        """
        captured = self.run_cli(
            "capture",
            "--type",
            "place",
            "--title",
            "Bar Vulkan",
            "--place-kind",
            "bar",
            "--text",
            "Notiz.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        self.rewrite_as_legacy_schema(path, 8, drop_fields={"interest_status"})

        applied = self.run_cli("migrate", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        content = path.read_text(encoding="utf-8")
        self.assertIn('"interest_status":"none"', content)

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_migration_refuses_to_write_an_entry_it_cannot_repair(
        self,
    ) -> None:
        """The preflight validates the *result*, not just the input.

        A required field with no default cannot be reconstructed. Writing
        anyway produced an entry that every later write command refused, with
        no rollback -- the only way out was hand-editing the Markdown.
        """
        captured = self.run_cli(
            "capture",
            "--type",
            "place",
            "--title",
            "Ohne Art",
            "--place-kind",
            "bar",
            "--text",
            "Notiz.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        legacy = self.rewrite_as_legacy_schema(
            path, 8, drop_fields={"place_kind"}
        )

        applied = self.run_cli("migrate", "--apply")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("after migration", applied.stdout)
        self.assertIn("no files were changed", applied.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), legacy)

    def test_migration_backup_dir_copies_entries_before_rewriting(
        self,
    ) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Sicherung",
            "--text",
            "Inhalt.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        legacy = self.rewrite_as_legacy_schema(path, 8)
        backup_dir = self.root / "migration-backup"

        applied = self.run_cli(
            "migrate", "--apply", "--backup-dir", str(backup_dir)
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        payload = json.loads(applied.stdout)
        self.assertEqual(payload["backed_up"], [result["created"]])
        self.assertEqual(
            (backup_dir / result["created"]).read_text(encoding="utf-8"),
            legacy,
        )
        self.assertIn(
            f"schema_version: {CURRENT_SCHEMA_VERSION}",
            path.read_text(encoding="utf-8"),
        )

        # A non-empty backup directory is refused rather than mixed into.
        again = self.run_cli(
            "migrate", "--apply", "--backup-dir", str(backup_dir)
        )
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("must be empty", again.stderr)

    def test_duplicate_frontmatter_keys_are_rejected(self) -> None:
        """Last-wins was silent, and `validate` did not report it either."""
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Erster Titel",
            "--text",
            "Inhalt.",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        content = path.read_text(encoding="utf-8")
        path.write_text(
            content.replace(
                'title: "Erster Titel"',
                'title: "Erster Titel"\ntitle: "Zweiter Titel"',
            ),
            encoding="utf-8",
        )
        validated = self.run_cli("validate")
        self.assertNotEqual(validated.returncode, 0)
        self.assertIn("duplicate frontmatter key", validated.stdout)

    def test_search_reports_entries_it_could_not_read(self) -> None:
        """`total: 0` used to mean both "no match" and "could not read"."""
        self.run_cli(
            "capture", "--type", "note", "--title", "Gut", "--text", "Inhalt."
        )
        broken = self.root / "vault" / "notes" / "broken.md"
        broken.write_text("﻿---\nid: 1\n---\n\nbomsecret\n", encoding="utf-8")

        searched = self.run_cli("search", "bomsecret")
        self.assertEqual(searched.returncode, 0, searched.stderr)
        payload = json.loads(searched.stdout)
        self.assertEqual(payload["total"], 0)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["unreadable_entry_count"], 1)

    def test_body_survives_a_write_without_losing_indentation(self) -> None:
        """An entry opening with an indented code block lost its indentation.

        `parse_frontmatter` stripped the body and `render_entry` rstripped it,
        so the first line's leading spaces disappeared on the next write of
        any kind -- and the code block rendered as prose from then on.
        """
        captured = self.run_cli(
            "capture",
            "--type",
            "note",
            "--title",
            "Codeblock",
            "--text",
            "    eingerueckte zeile\nzweite zeile",
        )
        result = json.loads(captured.stdout)
        path = self.root / result["created"]
        self.assertIn("    eingerueckte zeile", path.read_text(encoding="utf-8"))

        tagged = self.run_cli("tag", result["id"], "rum")
        self.assertEqual(tagged.returncode, 0, tagged.stderr)
        self.assertIn("    eingerueckte zeile", path.read_text(encoding="utf-8"))

    def test_trash_list_orders_by_instant_not_by_string(self) -> None:
        """`now_iso()` writes local time with an offset, not UTC.

        Sorting the raw strings ordered by wall-clock reading, so entries
        written either side of a timezone change came back reversed.
        """
        first = json.loads(
            self.run_cli(
                "capture", "--type", "note", "--title", "Frueher", "--text", "A."
            ).stdout
        )
        second = json.loads(
            self.run_cli(
                "capture", "--type", "note", "--title", "Spaeter", "--text", "B."
            ).stdout
        )
        self.run_cli("trash", first["id"], "--reason", "test")
        self.run_cli("trash", second["id"], "--reason", "test")

        first_path = next(
            (self.root / "trash").rglob(f"*{first['id'][3:11]}*.md")
        )
        second_path = next(
            (self.root / "trash").rglob(f"*{second['id'][3:11]}*.md")
        )
        # Same instant, different offsets: +09:00 is one hour *before* +00:00.
        self._set_field(first_path, "deleted_at", "2026-01-01T10:00:00+09:00")
        self._set_field(second_path, "deleted_at", "2026-01-01T09:00:00+00:00")

        listed = json.loads(self.run_cli("trash", "list").stdout)
        self.assertEqual(
            [item["id"] for item in listed],
            [second["id"], first["id"]],
        )

    def _set_field(self, path: Path, field: str, value: str) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith(f"{field}:"):
                lines[index] = f'{field}: "{value}"'
                break
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

class ZeroClawSecurityProfileTest(unittest.TestCase):
    def test_example_profiles_separate_vault_and_web_capabilities(self) -> None:
        path = (
            REPOSITORY_ROOT
            / "deploy"
            / "zeroclaw"
            / "security-profiles.example.toml"
        )
        with path.open("rb") as handle:
            config = tomllib.load(handle)

        knowledge = config["risk_profiles"]["knowledge_vault"]
        fetcher = config["risk_profiles"]["bookmark_fetcher"]
        self.assertEqual(knowledge["level"], "supervised")
        self.assertTrue(knowledge["workspace_only"])
        self.assertEqual(knowledge["allowed_commands"], [])
        self.assertEqual(knowledge["shell_env_passthrough"], [])
        self.assertLessEqual(
            {"tool_search", "read_skill", "delegate"},
            set(knowledge["allowed_tools"]),
        )
        self.assertEqual(
            knowledge["delegation_policy"]["mode"],
            "allow",
        )
        self.assertLessEqual(
            {
                "shell",
                "file_read",
                "file_write",
                "file_edit",
                "glob_search",
                "content_search",
                "http_request",
                "web_fetch",
                "browser",
            },
            set(knowledge["excluded_tools"]),
        )
        self.assertLessEqual(
            {
                "knowledge__inventory",
                "knowledge__search",
                "knowledge__get_entry",
                "knowledge__list_pending_attachments",
                "knowledge__review_queue",
                "knowledge__relations",
                "knowledge__list_trash",
                "knowledge__validate",
            },
            set(knowledge["auto_approve"]),
        )
        self.assertLessEqual(
            {
                "knowledge__capture",
                "knowledge__save_bookmark",
                "knowledge__save_recipe",
                "knowledge__update",
                "knowledge__add_attachment",
                "knowledge__trash",
                "knowledge__restore",
            },
            set(knowledge["always_ask"]),
        )

        servers = {
            server["name"]: server
            for server in config["mcp"]["servers"]
        }
        server = servers["knowledge"]
        self.assertEqual(server["name"], "knowledge")
        self.assertEqual(server["transport"], "stdio")
        self.assertEqual(server["command"], "/usr/bin/env")
        self.assertEqual(server["args"][0], "-i")
        self.assertIn("noetrail.mcp", server["args"])
        self.assertIn("PYTHONPATH=/srv/noetrail/src", server["args"])
        self.assertNotIn("/srv/noetrail/tools/knowledge_mcp.py", server["args"])
        self.assertEqual(
            config["mcp_bundles"]["knowledge_vault"]["servers"],
            ["knowledge"],
        )
        fetch_server = servers["bookmark_fetch"]
        self.assertEqual(fetch_server["transport"], "http")
        self.assertEqual(
            fetch_server["url"],
            "http://bookmark-fetcher:8080/mcp",
        )
        self.assertEqual(
            config["mcp_bundles"]["bookmark_fetcher"]["servers"],
            ["bookmark_fetch"],
        )

        self.assertEqual(
            set(fetcher["allowed_tools"]),
            {"tool_search", "read_skill", "bookmark_fetch__fetch"},
        )
        self.assertLessEqual(
            {
                "file_read",
                "file_write",
                "shell",
                "memory_recall",
                "web_search_tool",
                "web_fetch",
            },
            set(fetcher["excluded_tools"]),
        )
        self.assertFalse(config["link_enricher"]["enabled"])
        self.assertFalse(config["web_fetch"]["enabled"])
        self.assertFalse(config["web_search"]["enabled"])
        self.assertFalse(config["http_request"]["enabled"])
        self.assertFalse(config["browser"]["enabled"])
        self.assertTrue(config["secrets"]["encrypt"])
        self.assertTrue(config["security"]["leak_detection"]["enabled"])

    def test_example_profile_contains_no_secret_values(self) -> None:
        path = (
            REPOSITORY_ROOT
            / "deploy"
            / "zeroclaw"
            / "security-profiles.example.toml"
        )
        text = path.read_text(encoding="utf-8").casefold()
        self.assertNotIn("api_key =", text)
        self.assertNotIn("token =", text)
        self.assertNotIn("password =", text)


class SecretScannerTest(unittest.TestCase):
    def test_scanner_reports_location_but_never_secret_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = temporary_root(directory)
            secret = f"sk-proj-{'A' * 32}"
            (root / "note.md").write_text(
                f"accidental credential: {secret}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SECRET_SCRIPT), str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("note.md:1", result.stdout)
        self.assertIn("OpenAI-style API key", result.stdout)
        self.assertNotIn(secret, result.stdout)



if __name__ == "__main__":
    unittest.main()

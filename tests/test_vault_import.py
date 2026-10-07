"""End-to-end tests for `import obsidian` and `import basic-memory`.

The Obsidian fixture is a small but realistic vault: wikilinks in three
spellings, an image embed, an embed of a file type that cannot become an
attachment, inline and frontmatter tags, aliases, a daily note, a
`.obsidian/` configuration directory, a note whose frontmatter uses YAML the
importer refuses, a symbolic link, a canvas file, and a link that tries to
climb out of the vault.
"""

from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zlib

from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"

from noetrail.frontmatter import parse_frontmatter  # noqa: E402
from noetrail.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402


def one_pixel_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


class VaultImportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        initialized = self.run_cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.raw = self.data / "imports" / "raw"

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

    def write(self, relative: str, content: str) -> Path:
        path = self.raw / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def entries(self) -> dict[str, tuple[dict[str, object], str]]:
        found: dict[str, tuple[dict[str, object], str]] = {}
        for path in sorted((self.data / "vault").rglob("*.md")):
            metadata, body = parse_frontmatter(path)
            found[str(metadata["title"])] = (metadata, body)
        return found

    def build_obsidian_vault(self) -> None:
        """A synthetic vault with every construct the importer has to handle."""

        self.write(
            "vault/Index.md",
            "---\n"
            "title: My index\n"
            "tags:\n"
            "  - index\n"
            "  - Meta\n"
            "aliases:\n"
            "  - Home\n"
            "cssclasses:\n"
            "  - wide\n"
            "status: draft\n"
            "priority: 3\n"
            "---\n"
            "\n"
            "# My index\n"
            "\n"
            "Points at [[Projects/Apollo|the Apollo project]], "
            "[[Daily/2026-08-01]] and [[Notes/Deep#Section]].\n"
            "Mentions [[Missing Note]] and #inbox/to-read plus #1984.\n"
            "Climbing link: [[../../../etc/passwd]].\n"
            "\n"
            "![[diagram.png]]\n"
            "![[board.canvas]]\n"
            "![[fake.png]]\n"
            "\n"
            "```dataview\n"
            "LIST FROM #index\n"
            "```\n"
            "\n"
            "Not a link: `[[NotALink]]` and not a tag: `#nope`.\n",
        )
        self.write(
            "vault/Projects/Apollo.md",
            "---\ntags: [project, active]\n---\n\n"
            "# Apollo\n\nBack to [[Index]] and to [[Home]].\n"
            "Template leftovers: <% tp.date.now() %> and {{date}}.\n",
        )
        self.write(
            "vault/Notes/Deep.md",
            "# Deep note\n\nNothing links out of here.\n",
        )
        self.write(
            "vault/Daily/2026-08-01.md",
            "Worked on [[Projects/Apollo]] today. #daily\n",
        )
        self.write(
            "vault/Broken.md",
            "---\nalias: &anchor value\nother: *anchor\n---\n\nPathological.\n",
        )
        self.write("vault/board.canvas", '{"nodes":[],"edges":[]}')
        self.write("vault/notes.txt", "not markdown\n")
        (self.raw / "vault" / "Attachments").mkdir(parents=True, exist_ok=True)
        (self.raw / "vault" / "Attachments" / "diagram.png").write_bytes(
            one_pixel_png()
        )
        (self.raw / "vault" / "Attachments" / "fake.png").write_bytes(b"not an image")
        configuration = self.raw / "vault" / ".obsidian"
        configuration.mkdir(parents=True, exist_ok=True)
        (configuration / "appearance.json").write_text("{}", encoding="utf-8")
        (self.raw / "vault" / "leak.md").symlink_to("/etc/passwd")


class ObsidianImportTest(VaultImportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_obsidian_vault()

    def preview(self, *extra: str) -> dict[str, object]:
        result = self.run_cli("import", "obsidian", "--source", "vault", *extra)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return json.loads(result.stdout)

    def test_dry_run_reports_the_plan_and_writes_nothing(self) -> None:
        report = self.preview()
        self.assertTrue(report["dry_run"])
        counts = report["counts"]
        self.assertEqual(counts["markdown_files"], 5)
        self.assertEqual(counts["planned"], 4)
        self.assertEqual(counts["rejected"], 2)
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

    def test_apply_creates_valid_entries_with_relations_and_tags(self) -> None:
        applied = self.run_cli(
            "import", "obsidian", "--source", "vault", "--apply", "--tag", "migrated"
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertFalse(report["dry_run"])

        validated = self.run_cli("validate")
        self.assertEqual(
            validated.returncode, 0, validated.stdout + validated.stderr
        )

        entries = self.entries()
        self.assertEqual(
            set(entries), {"My index", "Apollo", "Deep note", "2026-08-01"}
        )
        index_metadata, index_body = entries["My index"]
        self.assertEqual(index_metadata["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(index_metadata["type"], "note")
        self.assertEqual(index_metadata["status"], "unreviewed")
        self.assertEqual(index_metadata["origin"], "import")

        # Frontmatter tags, an inline nested tag, and the CLI tag; `#1984` is
        # not a tag and neither is the one inside a code span.
        self.assertEqual(
            index_metadata["tags"],
            ["Meta", "inbox/to-read", "index", "migrated"],
        )

        # The body is preserved byte for byte apart from the property block.
        self.assertIn("![[diagram.png]]", index_body)
        self.assertNotIn("cssclasses", index_body)

        source = index_metadata["source"]
        assert isinstance(source, dict)
        self.assertEqual(source["system"], "obsidian")
        self.assertEqual(source["original_path"], "Index.md")
        self.assertEqual(source["aliases"], ["Home"])
        # Unmapped user properties survive; presentational ones do not.
        self.assertEqual(source["frontmatter"], {"status": "draft", "priority": 3})
        self.assertRegex(str(source["content_sha256"]), r"^[0-9a-f]{64}$")

    def test_wikilinks_become_relations_in_all_three_spellings(self) -> None:
        self.assertEqual(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).returncode,
            0,
        )
        entries = self.entries()
        identifiers = {
            title: metadata["id"] for title, (metadata, _) in entries.items()
        }
        index_relations = entries["My index"][0]["relations"]
        assert isinstance(index_relations, list)
        targets = {relation["target"] for relation in index_relations}
        self.assertEqual(
            targets,
            {
                identifiers["Apollo"],
                identifiers["2026-08-01"],
                identifiers["Deep note"],
            },
        )
        self.assertTrue(
            all(
                relation["predicate"] == "related_to"
                for relation in index_relations
            )
        )
        # An alias resolves to the aliased note rather than staying unresolved.
        apollo_relations = entries["Apollo"][0]["relations"]
        assert isinstance(apollo_relations, list)
        self.assertEqual(
            {relation["target"] for relation in apollo_relations},
            {identifiers["My index"]},
        )

    def test_unresolved_and_climbing_links_stay_visible_as_references(
        self,
    ) -> None:
        report = self.preview()
        self.assertIn("Missing Note", report["unresolved_link_targets"])
        self.assertEqual(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).returncode,
            0,
        )
        unresolved = self.entries()["My index"][0]["unresolved_relations"]
        assert isinstance(unresolved, list)
        references = {item["reference"] for item in unresolved}
        self.assertIn("Missing Note", references)
        # The traversal attempt is recorded as free text, never as a path.
        self.assertIn("../../../etc/passwd", references)
        self.assertFalse((self.data / "vault").joinpath("passwd").exists())

    def test_image_embed_becomes_a_content_addressed_attachment(self) -> None:
        self.assertEqual(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).returncode,
            0,
        )
        attachments = self.entries()["My index"][0]["attachments"]
        assert isinstance(attachments, list)
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment["media_type"], "image/png")
        self.assertEqual(attachment["origin"], "vault_import")
        self.assertEqual(attachment["original_name"], "diagram.png")
        blob = self.data / str(attachment["path"])
        self.assertTrue(blob.is_file())
        self.assertEqual(blob.read_bytes(), one_pixel_png())
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_non_image_and_canvas_embeds_are_reported_not_stored(self) -> None:
        report = self.preview()
        not_imported = report["not_imported"]
        assert isinstance(not_imported, dict)
        self.assertIn("board.canvas", not_imported["canvas"])
        self.assertIn("board.canvas", not_imported["unsupported_embeds"])
        self.assertIn("notes.txt", not_imported["unsupported_files"])
        self.assertEqual(
            not_imported["attachment_problems"],
            [
                {
                    "source": "Attachments/fake.png",
                    "reason": "file is not a supported image",
                }
            ],
        )
        self.assertEqual(not_imported["dataview_blocks"], 1)
        self.assertEqual(not_imported["templater_expressions"], 1)
        self.assertEqual(not_imported["core_template_placeholders"], 1)
        self.assertEqual(not_imported["dropped_properties"], ["cssclasses"])

    def test_symlink_and_pathological_frontmatter_are_skipped_and_named(
        self,
    ) -> None:
        report = self.preview()
        rejected = {item["source"]: item["reason"] for item in report["rejected"]}
        self.assertEqual(rejected["leak.md"], "symbolic link")
        self.assertIn("unsupported frontmatter", rejected["Broken.md"])
        # Nothing behind the link was read.
        self.assertNotIn("root:", json.dumps(report))

    def test_obsidian_configuration_is_detected_and_ignored(self) -> None:
        report = self.preview()
        ignored = report["ignored"]
        assert isinstance(ignored, dict)
        self.assertTrue(ignored["obsidian_config"])
        self.assertEqual(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).returncode,
            0,
        )
        stored = "".join(str(path) for path in (self.data / "vault").rglob("*"))
        self.assertNotIn("appearance", stored)

    def test_strict_refuses_the_whole_run_instead_of_skipping(self) -> None:
        result = self.run_cli(
            "import", "obsidian", "--source", "vault", "--strict", "--apply"
        )
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["created"], [])
        self.assertIn("--strict", json.dumps(report["errors"]))
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

    def test_repeating_an_unchanged_import_creates_nothing(self) -> None:
        first = self.run_cli("import", "obsidian", "--source", "vault", "--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        before = sorted(path.name for path in (self.data / "vault").rglob("*.md"))
        second = self.run_cli("import", "obsidian", "--source", "vault", "--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        report = json.loads(second.stdout)
        self.assertEqual(report["created"], [])
        self.assertEqual(len(report["skipped"]), 4)
        self.assertEqual(
            sorted(path.name for path in (self.data / "vault").rglob("*.md")),
            before,
        )

    def test_changed_source_is_a_conflict_and_leaves_the_entry_alone(
        self,
    ) -> None:
        self.assertEqual(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).returncode,
            0,
        )
        entry = next(
            path
            for path in (self.data / "vault").rglob("*.md")
            if "deep" in path.name
        )
        before = entry.read_bytes()
        self.write("vault/Notes/Deep.md", "# Deep note\n\nRewritten.\n")
        result = self.run_cli("import", "obsidian", "--source", "vault")
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn(
            "raw source changed", json.dumps(report["errors"])
        )
        self.assertEqual(entry.read_bytes(), before)

    def test_entry_ids_are_derived_from_the_relative_path(self) -> None:
        first = self.preview()
        identifiers = {item["source"]: item["id"] for item in first["created"]}
        self.assertEqual(
            identifiers,
            {item["source"]: item["id"] for item in self.preview()["created"]},
        )
        for entry_id in identifiers.values():
            self.assertRegex(entry_id, r"^kn_[0-9a-f]{32}$")

    def test_limit_selects_deterministically_and_reports_truncation(self) -> None:
        report = self.preview("--limit", "2")
        self.assertEqual(report["counts"]["selected"], 2)
        self.assertTrue(report["truncated"])
        self.assertEqual(
            self.preview("--limit", "2")["created"], report["created"]
        )

    def test_a_source_outside_imports_raw_is_refused(self) -> None:
        outside = self.base / "elsewhere"
        outside.mkdir()
        result = self.run_cli("import", "obsidian", "--source", str(outside))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("below imports/raw", result.stderr)

    def test_a_single_file_source_is_refused(self) -> None:
        result = self.run_cli("import", "obsidian", "--source", "vault/Index.md")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a directory", result.stderr)

    def test_provenance_marks_imported_values_as_the_user_s_own(self) -> None:
        self.assertEqual(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).returncode,
            0,
        )
        provenance = self.entries()["My index"][0]["provenance"]
        self.assertEqual(provenance, {"title": "user", "tags": "user"})
        # Nothing was fetched, so no body section is fenced as web content.
        self.assertNotIn("noetrail:web-content", self.entries()["My index"][1])

    def test_a_symlinked_directory_is_reported_and_never_walked(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("# Secret\n", encoding="utf-8")
        (self.raw / "vault" / "linked").symlink_to(outside, target_is_directory=True)
        report = self.preview()
        rejected = {item["source"] for item in report["rejected"]}
        self.assertIn("linked", rejected)
        self.assertNotIn("Secret", json.dumps(report))
        self.assertEqual(report["counts"]["markdown_files"], 5)

    def test_a_failed_write_rolls_back_every_entry_and_blob(self) -> None:
        """The `--apply` phase is all-or-nothing, including attachment blobs."""

        from noetrail.cli import run_command
        import noetrail.commands.vault_import as importer

        real = importer.atomic_write_entry
        calls = {"count": 0}

        def failing(*arguments: object, **keywords: object) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise KeyboardInterrupt("interrupted mid-import")
            real(*arguments, **keywords)  # type: ignore[arg-type]

        importer.atomic_write_entry = failing  # type: ignore[assignment]
        try:
            with self.assertRaises(KeyboardInterrupt):
                run_command(
                    [
                        "--data-root",
                        str(self.data),
                        "--config-root",
                        str(self.config),
                        "--builtins-root",
                        str(BUILTINS_ROOT),
                        "import",
                        "obsidian",
                        "--source",
                        "vault",
                        "--apply",
                    ]
                )
        finally:
            importer.atomic_write_entry = real  # type: ignore[assignment]

        self.assertGreaterEqual(calls["count"], 2)
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])
        attachments = self.data / "vault" / "attachments"
        # The shard directory proves a blob write was reached; the empty
        # listing proves the rollback removed what this run had stored.
        self.assertTrue(any(path.is_dir() for path in attachments.iterdir()))
        self.assertEqual(
            [path for path in attachments.rglob("*") if path.is_file()], []
        )
        # And the same import still succeeds afterwards.
        retried = self.run_cli("import", "obsidian", "--source", "vault", "--apply")
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_an_ambiguous_note_name_stays_unresolved(self) -> None:
        self.write("vault/One/Same.md", "# One\n\nLink to [[Index]].\n")
        self.write("vault/Two/Same.md", "# Two\n\nLink to [[Same]].\n")
        report = self.preview()
        self.assertIn("Same", report["unresolved_link_targets"])


class BasicMemoryImportTest(VaultImportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            "bm/coffee/pour-over.md",
            "---\n"
            "title: Pour Over Method\n"
            "type: note\n"
            "permalink: coffee/pour-over\n"
            "tags: [coffee, brewing]\n"
            "---\n"
            "\n"
            "Notes on pour over.\n"
            "\n"
            "## Observations\n"
            "- [method] Pour over highlights subtle flavors\n"
            "- [tip] Grind medium-fine for V60 #brewing\n"
            "- a bullet that is not an observation\n"
            "\n"
            "## Relations\n"
            "- pairs_well_with [[Chocolate Desserts]]\n"
            "- part_of [[Coffee Guide]]\n"
            "- based_on [[Coffee Guide]]\n",
        )
        self.write(
            "bm/coffee/guide.md",
            "---\ntitle: Coffee Guide\ntype: note\npermalink: coffee/guide\n---\n"
            "\nEverything about coffee.\n",
        )

    def test_documented_format_is_read_and_the_entries_validate(self) -> None:
        result = self.run_cli(
            "import", "basic-memory", "--source", "bm", "--apply"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["system"], "basic-memory")
        self.assertEqual(report["counts"]["planned"], 2)
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)

        entries = self.entries()
        metadata, _ = entries["Pour Over Method"]
        source = metadata["source"]
        assert isinstance(source, dict)
        self.assertEqual(source["permalink"], "coffee/pour-over")
        self.assertEqual(source["original_type"], "note")
        self.assertEqual(source["observation_categories"], ["method", "tip"])
        # An observation's own `#tag` joins the entry's tags.
        self.assertEqual(metadata["tags"], ["brewing", "coffee"])

    def test_relation_lines_keep_configured_predicates_and_report_the_rest(
        self,
    ) -> None:
        self.assertEqual(
            self.run_cli(
                "import", "basic-memory", "--source", "bm", "--apply"
            ).returncode,
            0,
        )
        entries = self.entries()
        guide_id = entries["Coffee Guide"][0]["id"]
        relations = entries["Pour Over Method"][0]["relations"]
        assert isinstance(relations, list)
        self.assertEqual(
            sorted(
                (relation["predicate"], relation["target"])
                for relation in relations
            ),
            sorted([("based_on", guide_id), ("part_of", guide_id)]),
        )
        unresolved = entries["Pour Over Method"][0]["unresolved_relations"]
        assert isinstance(unresolved, list)
        self.assertEqual(
            unresolved,
            [{"predicate": "related_to", "reference": "Chocolate Desserts"}],
        )

    def test_an_unconfigured_relation_type_is_named_in_the_report(self) -> None:
        report = json.loads(
            self.run_cli("import", "basic-memory", "--source", "bm").stdout
        )
        not_imported = report["not_imported"]
        assert isinstance(not_imported, dict)
        self.assertEqual(
            not_imported["substituted_relation_types"], ["pairs_well_with"]
        )

    def test_the_permalink_is_the_stable_source_identifier(self) -> None:
        first = json.loads(
            self.run_cli(
                "import", "basic-memory", "--source", "bm", "--apply"
            ).stdout
        )
        identifiers = {item["source"]: item["id"] for item in first["created"]}
        # Moving the file keeps the permalink, so the entry is recognised
        # instead of imported a second time.
        moved = self.raw / "bm" / "coffee" / "renamed.md"
        (self.raw / "bm" / "coffee" / "pour-over.md").rename(moved)
        second = json.loads(
            self.run_cli(
                "import", "basic-memory", "--source", "bm", "--apply"
            ).stdout
        )
        self.assertEqual(second["created"], [])
        self.assertEqual(len(second["skipped"]), 2)
        self.assertEqual(len(identifiers), 2)

    def test_obsidian_and_basic_memory_imports_do_not_see_each_other(
        self,
    ) -> None:
        self.build_obsidian_vault()
        self.assertEqual(
            self.run_cli(
                "import", "basic-memory", "--source", "bm", "--apply"
            ).returncode,
            0,
        )
        report = json.loads(
            self.run_cli(
                "import", "obsidian", "--source", "vault", "--apply"
            ).stdout
        )
        self.assertEqual(len(report["created"]), 4)
        self.assertEqual(report["skipped"], [])
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)


if __name__ == "__main__":
    unittest.main()

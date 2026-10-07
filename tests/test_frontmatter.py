"""Frontmatter is written in one dialect and read in the wider YAML subset.

The point of these tests is interoperability: an entry is a plain Markdown
file, so another tool may legitimately rewrite its frontmatter in block style.
Noetrail must still own that entry afterwards, and must refuse — rather than
guess at — anything whose meaning is not local to the value.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from noetrail.frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    parse_frontmatter_text,
    render_entry,
)
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def frontmatter(*lines: str) -> str:
    return "\n".join(["---", *lines, "---", "", "A body.", ""])


class CanonicalDialectTest(unittest.TestCase):
    def test_written_form_round_trips(self) -> None:
        metadata = {
            "id": "kn_" + "0" * 32,
            "schema_version": 9,
            "tags": ["a", "b"],
            "relations": [{"predicate": "mentions", "target": "kn_" + "1" * 32}],
            "attributes": {"author": "Ursula K. Le Guin"},
        }
        rendered = render_entry(metadata, "A body.")
        parsed, body = parse_frontmatter_text(rendered)
        self.assertEqual(parsed, metadata)
        self.assertEqual(body, "A body.")

    def test_body_may_contain_a_horizontal_rule(self) -> None:
        text = frontmatter('id: "kn_1"') + "\n---\n\nMore body.\n"
        parsed, body = parse_frontmatter_text(text)
        self.assertEqual(parsed, {"id": "kn_1"})
        self.assertIn("More body.", body)

    def test_empty_frontmatter_is_an_empty_mapping(self) -> None:
        parsed, _ = parse_frontmatter_text("---\n---\n\nA body.\n")
        self.assertEqual(parsed, {})

    def test_body_indentation_survives_the_tolerant_path(self) -> None:
        # An entry whose body opens with an indented code block keeps that
        # indentation. The canonical reader already guaranteed this; the block
        # reader must not quietly reintroduce the old `.strip()`.
        text = "---\ntags:\n  - demo\n---\n\n    indented code\n\ntext\n"
        _, body = parse_frontmatter_text(text)
        self.assertEqual(body, "    indented code\n\ntext")

    def test_file_is_read_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "entry.md"
            path.write_text(frontmatter('title: "Read me"'), encoding="utf-8")
            parsed, body = parse_frontmatter(path)
        self.assertEqual(parsed, {"title": "Read me"})
        self.assertEqual(body, "A body.")


class ExternalDialectTest(unittest.TestCase):
    """Forms ordinary Markdown and YAML tools produce when they rewrite a file."""

    def assertParses(self, text: str, expected: dict[str, object]) -> None:
        parsed, _ = parse_frontmatter_text(text)
        self.assertEqual(parsed, expected)

    def test_indented_block_sequence(self) -> None:
        self.assertParses(
            frontmatter("tags:", "  - demo", "  - travel"),
            {"tags": ["demo", "travel"]},
        )

    def test_block_sequence_at_the_parent_column(self) -> None:
        self.assertParses(
            frontmatter("tags:", "- demo", "- travel"),
            {"tags": ["demo", "travel"]},
        )

    def test_unquoted_scalars(self) -> None:
        self.assertParses(
            frontmatter("id: kn_1", "title: A short title", "schema_version: 9"),
            {"id": "kn_1", "title": "A short title", "schema_version": 9},
        )

    def test_single_quoted_scalars(self) -> None:
        self.assertParses(
            frontmatter("id: 'kn_1'", "title: 'it''s fine'"),
            {"id": "kn_1", "title": "it's fine"},
        )

    def test_comments(self) -> None:
        self.assertParses(
            frontmatter("# a standalone comment", 'id: "kn_1"  # trailing', "n: 3 # c"),
            {"id": "kn_1", "n": 3},
        )

    def test_hash_without_a_leading_space_stays_in_the_value(self) -> None:
        self.assertParses(frontmatter("title: rum#3"), {"title": "rum#3"})

    def test_sequence_of_mappings(self) -> None:
        self.assertParses(
            frontmatter(
                "relations:",
                "  - predicate: mentions",
                "    target: kn_2",
                "  - predicate: about",
                "    target: kn_3",
            ),
            {
                "relations": [
                    {"predicate": "mentions", "target": "kn_2"},
                    {"predicate": "about", "target": "kn_3"},
                ]
            },
        )

    def test_nested_block_mapping(self) -> None:
        self.assertParses(
            frontmatter("source:", "  system: notion", '  original_id: "abc"'),
            {"source": {"system": "notion", "original_id": "abc"}},
        )

    def test_core_scalar_types(self) -> None:
        self.assertParses(
            frontmatter("a: true", "b: false", "c: null", "d: ~", "e:", "f: -2.5"),
            {"a": True, "b": False, "c": None, "d": None, "e": None, "f": -2.5},
        )

    def test_colon_inside_a_plain_value_is_not_a_key(self) -> None:
        self.assertParses(
            frontmatter(
                "url: https://example.com/a:b",
                "created_at: 2026-08-07T10:00:00+00:00",
            ),
            {
                "url": "https://example.com/a:b",
                "created_at": "2026-08-07T10:00:00+00:00",
            },
        )

    def test_block_scalars(self) -> None:
        self.assertParses(
            frontmatter("note: |", "  first", "  second", "other: >-", "  a", "  b"),
            {"note": "first\nsecond\n", "other": "a b"},
        )

    def test_document_end_marker_closes_the_frontmatter(self) -> None:
        parsed, body = parse_frontmatter_text("---\nid: kn_1\n...\n\nA body.\n")
        self.assertEqual(parsed, {"id": "kn_1"})
        self.assertEqual(body, "A body.")


class RefusedConstructTest(unittest.TestCase):
    """Constructs that could silently change a value must fail, not be guessed."""

    def assertRefused(self, text: str, fragment: str) -> None:
        with self.assertRaises(FrontmatterError) as raised:
            parse_frontmatter_text(text)
        self.assertIn(fragment, str(raised.exception))

    def test_anchor(self) -> None:
        self.assertRefused(frontmatter("a: &anchor x"), "anchors")

    def test_alias(self) -> None:
        self.assertRefused(frontmatter("a: *anchor"), "anchors")

    def test_tag(self) -> None:
        self.assertRefused(frontmatter("a: !!python/object x"), "anchors")

    def test_merge_key(self) -> None:
        self.assertRefused(frontmatter("<<: other"), "anchors")

    def test_duplicate_key_in_either_dialect(self) -> None:
        self.assertRefused(frontmatter('id: "a"', 'id: "b"'), "duplicate")
        self.assertRefused(frontmatter("id: a", "id: b"), "duplicate")

    def test_multi_line_plain_scalar(self) -> None:
        self.assertRefused(
            frontmatter("title: a very long", "  continued title"),
            "unexpected indentation",
        )

    def test_tab_indentation(self) -> None:
        self.assertRefused(frontmatter("tags:", "\t- a"), "tabs")

    def test_control_characters(self) -> None:
        self.assertRefused(frontmatter("title: a\x07b"), "control characters")

    def test_trailing_text_after_a_quoted_value(self) -> None:
        self.assertRefused(frontmatter('title: "a" b'), "unexpected text")

    def test_missing_delimiters(self) -> None:
        self.assertRefused("---\nid: a\n", "closing")
        self.assertRefused("no frontmatter\n", "opening")

    def test_line_that_is_not_a_mapping_entry(self) -> None:
        self.assertRefused(frontmatter("not a mapping entry"), "key: value")

    def test_frontmatter_with_too_many_lines(self) -> None:
        self.assertRefused(
            frontmatter(*(f"key_{index}: {index}" for index in range(5_000))),
            "too many lines",
        )


class ExternallyRewrittenEntryTest(unittest.TestCase):
    """A tool may rewrite an entry; Noetrail must still read, edit, and normalize it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        (self.root / ".knowledge").mkdir()
        (self.root / ".knowledge" / "SPEC.md").write_text("test", encoding="utf-8")
        (self.root / ".knowledge" / "relation-types.yaml").write_text(
            "relations:\n  related_to:\n    symmetric: true\n",
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

    def entry_path(self) -> Path:
        paths = sorted((self.root / "vault").rglob("*.md"))
        self.assertEqual(len(paths), 1, paths)
        return paths[0]

    def test_block_style_entry_stays_readable_editable_and_is_normalized(self) -> None:
        captured = self.run_cli(
            "capture",
            "--type",
            "thought",
            "--title",
            "Portable entry",
            "--text",
            "A thought that another tool will rewrite.",
            "--tag",
            "demo",
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        entry_id = json.loads(captured.stdout)["id"]

        path = self.entry_path()
        metadata, body = parse_frontmatter(path)
        # Rewrite the frontmatter the way an ordinary YAML tool would.
        rewritten = ["---"]
        for key, value in metadata.items():
            if isinstance(value, list) and value and all(
                isinstance(item, str) for item in value
            ):
                rewritten.append(f"{key}:")
                rewritten.extend(f"  - {item}" for item in value)
            elif isinstance(value, str):
                rewritten.append(f"{key}: {value}")
            else:
                rewritten.append(
                    f"{key}: "
                    + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                )
        rewritten.extend(["---", "", body, ""])
        path.write_text("\n".join(rewritten), encoding="utf-8")

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

        found = self.run_cli("search", "rewrite")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(json.loads(found.stdout)["total"], 1)

        # The BM25 index reads entries through the same parser, so a rewritten
        # entry has to be indexable rather than silently dropped from the
        # index while the substring scan still finds it.
        rebuilt = self.run_cli("index", "rebuild")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(json.loads(rebuilt.stdout)["documents"], 1)
        ranked = self.run_cli("search", "--rank", "bm25", "rewrite")
        self.assertEqual(ranked.returncode, 0, ranked.stderr)
        self.assertEqual(json.loads(ranked.stdout)["total"], 1)

        updated = self.run_cli("tag", entry_id, "portable")
        self.assertEqual(updated.returncode, 0, updated.stderr)

        # The next write restores the canonical dialect.
        text = self.entry_path().read_text(encoding="utf-8")
        self.assertIn('tags: ["demo","portable"]', text)
        self.assertNotIn("\n  - demo", text)


if __name__ == "__main__":
    unittest.main()

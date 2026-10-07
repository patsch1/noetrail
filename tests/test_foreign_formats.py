"""Unit tests for the Obsidian and Basic Memory format readers.

These sit below `tests/test_vault_import.py`: that file checks what the
importer produces, this one checks the individual reading rules, including
the ones a real vault only exercises rarely.
"""

from __future__ import annotations

import unittest

from noetrail.basic_memory import observation_tags, parse_sections
from noetrail.obsidian import (
    ObsidianFrontmatterError,
    first_heading,
    inline_tags,
    lossy_constructs,
    normalize_tag,
    parse_properties,
    split_frontmatter,
    strip_code,
    wikilinks,
)


class FrontmatterSplitTest(unittest.TestCase):
    def test_a_block_is_only_recognised_on_the_first_line(self) -> None:
        block, body = split_frontmatter("---\ntitle: A\n---\n\nBody.\n")
        self.assertEqual(block, "title: A")
        self.assertEqual(body, "Body.")

        block, body = split_frontmatter("Text.\n\n---\ntitle: A\n---\n")
        self.assertIsNone(block)
        self.assertEqual(body, "Text.\n\n---\ntitle: A\n---\n")

    def test_an_unterminated_block_stays_body(self) -> None:
        block, body = split_frontmatter("---\ntitle: A\n\nstill text\n")
        self.assertIsNone(block)
        self.assertIn("title: A", body)

    def test_a_horizontal_rule_in_the_body_is_not_a_delimiter(self) -> None:
        _, body = split_frontmatter("---\ntitle: A\n---\n\nOne\n\n---\n\nTwo\n")
        self.assertEqual(body, "One\n\n---\n\nTwo")


class PropertyParserTest(unittest.TestCase):
    def test_the_shapes_a_real_vault_uses_are_read(self) -> None:
        parsed = parse_properties(
            "title: My note\n"
            "tags:\n"
            "  - one\n"
            "  - two\n"
            "aliases: [Home, Start]\n"
            'quoted: "a: colon"\n'
            "single: 'it''s fine'\n"
            "count: 12\n"
            "ratio: 1.5\n"
            "flag: true\n"
            "nothing: null\n"
            "# a comment\n"
            "date property: 2026-08-01\n"
        )
        self.assertEqual(
            parsed,
            {
                "title": "My note",
                "tags": ["one", "two"],
                "aliases": ["Home", "Start"],
                "quoted": "a: colon",
                "single": "it's fine",
                "count": 12,
                "ratio": 1.5,
                "flag": True,
                "nothing": None,
                "date property": "2026-08-01",
            },
        )

    def test_the_refused_yaml_constructs_name_themselves_and_a_line(
        self,
    ) -> None:
        for block, marker in (
            ("a: &anchor value\n", "anchors"),
            ("a: *alias\n", "anchors"),
            ("a: !!python/object x\n", "anchors"),
            ("a: |\n  block\n", "anchors"),
            ("a: >\n  folded\n", "anchors"),
            ("a: <<\n", "anchors"),
            ("a:\n  b: nested\n", "nested mappings"),
            ("a\tb: 1\n", "tabs"),
            ("- orphan\n", "list item without a property name"),
            ("no colon here\n", "expected 'name: value'"),
            ("a: 1\na: 2\n", "duplicate property"),
            ("a b/c: 1\n", "invalid property name"),
        ):
            with self.subTest(block=block):
                with self.assertRaises(ObsidianFrontmatterError) as caught:
                    parse_properties(block)
                self.assertIn(marker, caught.exception.reason)
                self.assertGreaterEqual(caught.exception.line, 1)

    def test_the_block_is_bounded(self) -> None:
        with self.assertRaises(ObsidianFrontmatterError):
            parse_properties("\n".join(f"k{index}: 1" for index in range(500)))
        with self.assertRaises(ObsidianFrontmatterError):
            parse_properties("a: " + "x" * 9000)


class WikilinkTest(unittest.TestCase):
    def test_every_documented_spelling_is_read(self) -> None:
        found = wikilinks(
            "[[Plain]] [[Note|Display]] [[Note#Heading]] [[Note#^block]] "
            "![[Image.png]] [[folder/Deep|d]]"
        )
        self.assertEqual(
            [(link.embed, link.target, link.subpath, link.display) for link in found],
            [
                (False, "Plain", "", ""),
                (False, "Note", "", "Display"),
                (False, "Note", "Heading", ""),
                (False, "Note", "^block", ""),
                (True, "Image.png", "", ""),
                (False, "folder/Deep", "", "d"),
            ],
        )

    def test_links_inside_code_are_not_links(self) -> None:
        self.assertEqual(wikilinks("`[[No]]`"), [])
        self.assertEqual(wikilinks("```\n[[No]]\n```\n"), [])
        self.assertEqual(wikilinks("~~~text\n[[No]]\n~~~\n"), [])
        self.assertEqual(len(wikilinks("```\n[[No]]\n```\n\n[[Yes]]\n")), 1)


class TagTest(unittest.TestCase):
    def test_the_documented_tag_rules_are_applied(self) -> None:
        self.assertEqual(normalize_tag("#project"), "project")
        self.assertEqual(normalize_tag("inbox/to-read"), "inbox/to-read")
        self.assertEqual(normalize_tag("snake_case"), "snake_case")
        # "Tags must contain at least one non-numerical character."
        self.assertIsNone(normalize_tag("1984"))
        self.assertIsNone(normalize_tag("two words"))
        self.assertIsNone(normalize_tag(""))
        self.assertIsNone(normalize_tag(True))

    def test_inline_tags_exclude_headings_anchors_and_code(self) -> None:
        self.assertEqual(
            inline_tags(
                "# Heading\n"
                "text #alpha and #beta/gamma\n"
                "url https://host/page#anchor\n"
                "`#incode`\n"
                "#1984\n"
            ),
            ["alpha", "beta/gamma"],
        )


class LossyConstructTest(unittest.TestCase):
    def test_the_constructs_that_lose_behaviour_are_counted(self) -> None:
        report = lossy_constructs(
            "```dataview\nLIST\n```\n"
            "```dataviewjs\ndv.list([])\n```\n"
            "inline `= this.file.name`\n"
            "<% tp.date.now() %>\n"
            "{{date}} {{ title }}\n"
        )
        self.assertEqual(report.dataview, 3)
        self.assertEqual(report.templater, 1)
        self.assertEqual(report.core_templates, 2)

    def test_strip_code_keeps_the_line_count(self) -> None:
        text = "a\n```\nb\nc\n```\nd\n"
        self.assertEqual(
            len(strip_code(text).splitlines()), len(text.splitlines())
        )


class HeadingTest(unittest.TestCase):
    def test_only_a_level_one_heading_becomes_a_title(self) -> None:
        self.assertEqual(first_heading("## Two\n\n# One\n"), "One")
        self.assertIsNone(first_heading("## Two\n\ntext\n"))


class BasicMemorySectionTest(unittest.TestCase):
    NOTE = (
        "Intro with [[A Link]].\n"
        "\n"
        "## Observations\n"
        "- [decision] Using JWT tokens #security\n"
        "- [approach] Refresh tokens in cookies (per audit)\n"
        "- not an observation\n"
        "\n"
        "## Relations\n"
        "- implements [[API Security Requirements]]\n"
        "- depends_on [[User Database Schema#Users]]\n"
        "- malformed line\n"
    )

    def test_the_documented_section_syntax_is_read(self) -> None:
        sections = parse_sections(self.NOTE)
        self.assertEqual(
            [(item.category, item.content) for item in sections.observations],
            [
                ("decision", "Using JWT tokens #security"),
                ("approach", "Refresh tokens in cookies (per audit)"),
            ],
        )
        self.assertEqual(
            [
                (item.relation_type, item.target, item.subpath)
                for item in sections.relations
            ],
            [
                ("implements", "API Security Requirements", ""),
                ("depends_on", "User Database Schema", "Users"),
            ],
        )
        self.assertEqual(sections.unparsed_lines, 2)

    def test_section_membership_decides_what_is_a_relation(self) -> None:
        outside = parse_sections("- implements [[Elsewhere]]\n")
        self.assertEqual(outside.relations, [])
        self.assertEqual(outside.observations, [])

    def test_observation_tags_are_collected(self) -> None:
        self.assertEqual(
            observation_tags(parse_sections(self.NOTE).observations), ["security"]
        )


if __name__ == "__main__":
    unittest.main()

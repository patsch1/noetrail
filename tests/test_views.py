"""Saved views are configuration that must not become a capability.

A view is read-only declarative search stored in the instance config root. The
whole value of that is what it *cannot* say: no shell, no path, no URL, no
hook, no unbounded value. That guarantee lives entirely in the parser's
refusals, so a refusal that never runs in a test is a guarantee nobody has
checked.

The second half of this file is the accepting side. A field that is parsed but
never exercised end to end is the other way this goes wrong: the file loads,
the view lists, and the filter it declares quietly does nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from noetrail import views
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class SavedViewsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        knowledge = self.root / ".knowledge"
        knowledge.mkdir()
        (knowledge / "SPEC.md").write_text("synthetic", encoding="utf-8")
        (knowledge / "relation-types.yaml").write_text(
            "relations:\n  related_to:\n    symmetric: true\n",
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge/packs/knowledge-core",
            knowledge / "packs/knowledge-core",
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

    def write_views(self, body: str) -> None:
        (self.root / ".knowledge/views.yaml").write_text(body, encoding="utf-8")

    def view_body(self, *lines: str) -> None:
        """Exactly these lines under `views: probe:`, nothing implied."""

        self.write_views(
            "format_version: 1\nviews:\n  probe:\n"
            + "".join(f"    {line}\n" for line in lines)
        )

    def one_view(self, *lines: str) -> None:
        """A minimal valid view, plus the lines under test."""

        self.view_body('title: "Probe"', *lines)

    def refuse(self, *arguments: str) -> str:
        completed = self.run_cli(*arguments)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return completed.stderr

    def accept(self, *arguments: str) -> dict:
        completed = self.run_cli(*arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_list_and_run_apply_bounded_declared_filters(self) -> None:
        for title, kind, status in (
            ("Unread article", "article", "unread"),
            ("Read article", "article", "read"),
            ("Unread tool", "tool", "unread"),
        ):
            payload = self.root / f"{title}.json"
            payload.write_text(
                json.dumps(
                    {
                        "url": f"https://example.invalid/{title.replace(' ', '-')}",
                        "title": title,
                        "bookmark_kind": kind,
                        "reading_status": status,
                    }
                ),
                encoding="utf-8",
            )
            created = self.run_cli(
                "bookmark",
                "--metadata-file",
                str(payload),
            )
            self.assertEqual(created.returncode, 0, created.stderr)

        (self.root / ".knowledge/views.yaml").write_text(
            "format_version: 1\n"
            "views:\n"
            "  unread_articles:\n"
            '    title: "Unread articles"\n'
            '    description: "Articles retained for later."\n'
            '    query: ""\n'
            '    type: "bookmark"\n'
            '    bookmark_kind: "article"\n'
            '    reading_status: "unread"\n'
            '    sort: "title_asc"\n'
            "    limit: 20\n",
            encoding="utf-8",
        )

        listed = self.run_cli("view", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        summary = json.loads(listed.stdout)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["views"][0]["name"], "unread_articles")

        run = self.run_cli("view", "run", "unread_articles")
        self.assertEqual(run.returncode, 0, run.stderr)
        page = json.loads(run.stdout)
        self.assertEqual(page["view"], "unread_articles")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["title"], "Unread article")
        # A view that names no ranking mode gets the one an ordinary search
        # gets, rather than a second copy of a default that can drift.
        self.assertEqual(summary["views"][0]["search"]["rank"], "hybrid")

    def test_invalid_or_executable_view_fields_are_rejected(self) -> None:
        (self.root / ".knowledge/views.yaml").write_text(
            "format_version: 1\n"
            "views:\n"
            "  unsafe:\n"
            '    title: "Unsafe"\n'
            '    shell: "curl https://example.invalid"\n',
            encoding="utf-8",
        )
        refused = self.run_cli("view", "list")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("unknown keys: shell", refused.stderr)

    def test_view_file_must_not_be_a_symlink(self) -> None:
        target = self.root / "outside.yaml"
        target.write_text("format_version: 1\nviews: {}\n", encoding="utf-8")
        (self.root / ".knowledge/views.yaml").symlink_to(target)
        refused = self.run_cli("view", "list")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("must not be a symbolic link", refused.stderr)

    # -- the file as a whole -------------------------------------------

    def test_no_file_means_no_views_rather_than_an_error(self) -> None:
        listed = self.accept("view", "list")
        self.assertEqual(listed["count"], 0)
        self.assertEqual(listed["views"], [])
        self.assertIn("Unknown saved view", self.refuse("view", "run", "absent"))

    def test_the_file_envelope_is_checked_before_anything_in_it(self) -> None:
        for body, expected in (
            ("format_version: 2\nviews: {}\n", "format_version must be 1"),
            ("views: {}\n", "format_version must be 1"),
            (
                "format_version: 1\nviews: {}\nexec: true\n",
                "unknown keys: exec",
            ),
            ("format_version: 1\n", "must be a mapping"),
            ("format_version: 1\nviews: []\n", "must be a mapping"),
            ("format_version: 1\nviews:\n  probe: 3\n", "must be a mapping"),
        ):
            with self.subTest(body=body):
                self.write_views(body)
                self.assertIn(expected, self.refuse("view", "list"))

    def test_the_file_is_bounded_in_size_and_in_count(self) -> None:
        # A config file is read before anything else can refuse it, so its own
        # size is the first bound there is.
        self.write_views(
            "format_version: 1\nviews:\n  probe:\n"
            '    title: "Probe"\n'
            f'    description: "{"x" * 300_000}"\n'
        )
        self.assertIn("exceed", self.refuse("view", "list"))

        self.write_views(
            "format_version: 1\nviews:\n"
            + "".join(
                f'  probe_{index}:\n    title: "Probe {index}"\n'
                for index in range(129)
            )
        )
        self.assertIn("at most 128", self.refuse("view", "list"))

    def test_a_view_name_is_an_identifier_and_never_a_path(self) -> None:
        """Refused twice, and the outer one is the one that fires.

        `VIEW_NAME_PATTERN` is character-for-character the `FIELD_PATTERN` the
        restricted YAML reader applies to every mapping key, so a bad name
        never reaches the view parser through a file. The check is kept as the
        second line of defence for a caller that is not the file reader, and
        it is exercised directly here because that is the only way to reach
        it -- if it ever stops being redundant, this is what will still hold.
        """

        for name in ("../escape", "with space", "Upper", "9leading", "a" * 64):
            with self.subTest(name=name):
                self.write_views(
                    "format_version: 1\nviews:\n"
                    f"  {name}:\n    title: \"Probe\"\n"
                )
                self.assertIn("invalid mapping key", self.refuse("view", "list"))
                with self.assertRaises(views.ViewError) as raised:
                    views._parse_view(name, {"title": "Probe"}, None)
                self.assertIn("invalid saved view name", str(raised.exception))

    def test_a_view_carries_no_key_that_could_reach_outside_the_file(self) -> None:
        # The list is not exhaustive by construction -- anything absent from
        # VIEW_KEYS is refused -- but these are the shapes a config file grows
        # when nobody is enforcing that.
        for key, value in (
            ("command", '"noetrail purge"'),
            ("path", '"../../etc/passwd"'),
            ("url", '"https://example.invalid/hook"'),
            ("data_root", '"/srv/elsewhere"'),
            ("on_run", '"post.sh"'),
            ("rerank_vectors", '"/tmp/vectors.jsonl"'),
        ):
            with self.subTest(key=key):
                self.one_view(f"{key}: {value}")
                self.assertIn(
                    f"unknown keys: {key}", self.refuse("view", "list")
                )

    # -- the fields ----------------------------------------------------

    def test_every_bounded_field_refuses_what_it_does_not_allow(self) -> None:
        for line, expected in (
            (f'description: "{"x" * 1100}"', "must not exceed"),
            ("query: 3", "query must be a string"),
            (f'query: "{"x" * 2100}"', "query must be a string"),
            ("type: 3", "type must be a string"),
            ('type: "invented"', "invented"),
            ('reading_status: "devoured"', "invalid reading_status"),
            ('bookmark_kind: "scroll"', "invalid bookmark_kind"),
            ('place_kind: "moon"', "invalid place_kind"),
            ('product_kind: "widget"', "invalid product_kind"),
            ('experience_kind: "levitating"', "invalid experience_kind"),
            ('interest_status: "someday"', "invalid interest_status"),
            ('sort: "by_vibes"', "invalid sort"),
            ('rank: "semantic"', "invalid rank"),
            ("rank: 3", "invalid rank"),
            ('related_id: "not-an-id"', "invalid related_id"),
            ("related_id: 3", "invalid related_id"),
            ('relation_predicate: "Involves"', "invalid relation_predicate"),
            ('relation_predicate: "../involves"', "invalid relation_predicate"),
            ('experienced: "yes"', "experienced must be a boolean"),
            ("min_rating: 0", "min_rating must be from 1 to 5"),
            ("min_rating: 6", "min_rating must be from 1 to 5"),
            ("min_rating: true", "min_rating must be from 1 to 5"),
            ('min_rating: "4"', "min_rating must be from 1 to 5"),
            ("limit: 0", "limit must be from 1 to 50"),
            ("limit: 51", "limit must be from 1 to 50"),
            ("limit: true", "limit must be from 1 to 50"),
            ("domain: 3", "domain must be a non-empty string"),
            (f'domain: "{"d" * 300}"', "must not exceed"),
        ):
            with self.subTest(line=line):
                self.one_view(line)
                self.assertIn(expected, self.refuse("view", "list"))

    def test_a_view_without_a_usable_title_is_refused(self) -> None:
        for line, expected in (
            ('title: ""', "title must be a non-empty string"),
            ("title: 3", "title must be a non-empty string"),
            ("title: []", "title must be a non-empty string"),
            # Absent entirely: the field is required, not defaulted.
            ('query: "x"', "title must be a non-empty string"),
            (f'title: "{"t" * 250}"', "title must not exceed 200 characters"),
        ):
            with self.subTest(line=line):
                self.view_body(line)
                self.assertIn(expected, self.refuse("view", "list"))

    def test_a_typed_attribute_filter_is_checked_against_its_pack(self) -> None:
        pack = self.root / ".knowledge/packs/books"
        pack.mkdir(parents=True)
        (pack / "pack.yaml").write_text(
            "format_version: 1\n"
            'id: "books"\n'
            "version: 1\n"
            'title: "Books"\n'
            'description: "Synthetic pack for saved-view tests."\n'
            "types:\n"
            "  book:\n"
            '    title: "Book"\n'
            '    description: "A synthetic book."\n'
            "    fields:\n"
            "      author:\n"
            '        type: "string"\n'
            "        required: true\n"
            "        searchable: true\n"
            "      pages:\n"
            '        type: "integer"\n'
            "        minimum: 1\n",
            encoding="utf-8",
        )

        for lines, expected in (
            (
                ('attribute_filters:', '  author: "Someone"'),
                "attribute_filters require a pack-defined type",
            ),
            (
                ('type: "books/book"', "attribute_filters: []"),
                "must be a mapping",
            ),
            (
                ('type: "books/book"', "attribute_filters:", '  invented: "x"'),
                "unknown attribute filter invented",
            ),
            (
                ('type: "books/book"', "attribute_filters:", "  pages: 3"),
                "pages is not searchable",
            ),
            (
                ('type: "books/book"', "attribute_filters:", "  author: 3"),
                "author",
            ),
        ):
            with self.subTest(lines=lines):
                self.one_view(*lines)
                self.assertIn(expected, self.refuse("view", "list"))

    def test_a_typed_attribute_filter_actually_filters(self) -> None:
        """The accepting half: a declared filter that selects nothing would
        pass every refusal test above and still be broken."""

        pack = self.root / ".knowledge/packs/books"
        pack.mkdir(parents=True)
        (pack / "pack.yaml").write_text(
            "format_version: 1\n"
            'id: "books"\n'
            "version: 1\n"
            'title: "Books"\n'
            'description: "Synthetic pack for saved-view tests."\n'
            "types:\n"
            "  book:\n"
            '    title: "Book"\n'
            '    description: "A synthetic book."\n'
            "    fields:\n"
            "      author:\n"
            '        type: "string"\n'
            "        required: true\n"
            "        searchable: true\n",
            encoding="utf-8",
        )
        for title, author in (("Atlas", "Rivera"), ("Beacon", "Okonkwo")):
            payload = self.root / f"{title}.json"
            payload.write_text(json.dumps({"author": author}), encoding="utf-8")
            created = self.run_cli(
                "capture",
                "--type",
                "books/book",
                "--title",
                title,
                "--attributes-file",
                str(payload),
                "--text",
                "Synthetic body.",
            )
            self.assertEqual(created.returncode, 0, created.stderr)

        self.one_view(
            'type: "books/book"',
            "attribute_filters:",
            '  author: "Rivera"',
        )
        page = self.accept("view", "run", "probe")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["title"], "Atlas")

    def test_the_relation_and_rating_filters_reach_the_search(self) -> None:
        first = json.loads(
            self.run_cli(
                "capture", "--type", "note", "--title", "Subject", "--text", "One."
            ).stdout
        )["id"]
        event = json.loads(
            self.run_cli(
                "capture",
                "--type",
                "experience",
                "--experience-kind",
                "reading",
                "--rating",
                "5",
                "--relation",
                f"related_to:{first}",
                "--title",
                "Read it",
                "--text",
                "Synthetic event.",
            ).stdout
        )["id"]
        self.one_view(
            'type: "experience"',
            f'related_id: "{first}"',
            'relation_predicate: "related_to"',
            "min_rating: 4",
            'sort: "occurred_desc"',
            "limit: 5",
        )
        page = self.accept("view", "run", "probe")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], event)

        # And the same view with a rating nothing reaches selects nothing,
        # which is what proves the filter was applied rather than ignored.
        self.one_view(
            'type: "experience"',
            f'related_id: "{first}"',
            'relation_predicate: "related_to"',
            "min_rating: 4",
            'experienced: true',
        )
        self.assertEqual(self.accept("view", "run", "probe")["total"], 0)

    def test_a_file_that_cannot_be_decoded_is_refused_not_ignored(self) -> None:
        # An unreadable config file must not fall back to "no views". Silence
        # would look identical to an empty file.
        (self.root / ".knowledge/views.yaml").write_bytes(
            b"format_version: 1\nviews:\n  probe:\n    title: \"\xff\xfe\"\n"
        )
        self.assertIn("could not read saved views", self.refuse("view", "list"))

    def test_the_string_valued_filters_reach_the_search(self) -> None:
        """`domain` and the instant-valued fields are copied through as text.

        They are the fields whose accepting path does nothing visible in the
        summary, so a view could declare one and quietly not apply it.
        """

        for title, host in (("Kept", "example.invalid"), ("Other", "other.invalid")):
            payload = self.root / f"{title}.json"
            payload.write_text(
                json.dumps({"url": f"https://{host}/{title}", "title": title}),
                encoding="utf-8",
            )
            created = self.run_cli("bookmark", "--metadata-file", str(payload))
            self.assertEqual(created.returncode, 0, created.stderr)

        self.one_view('type: "bookmark"', 'domain: "example.invalid"')
        page = self.accept("view", "run", "probe")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["title"], "Kept")

        # An instant in the past: valid time is evaluated then, and both
        # bookmarks were created after it.
        self.one_view('type: "bookmark"', 'as_of: "2000-01-01T00:00:00+00:00"')
        self.assertEqual(self.accept("view", "run", "probe")["total"], 2)

    def test_a_run_may_narrow_the_page_but_not_the_view(self) -> None:
        for index in range(3):
            self.run_cli(
                "capture",
                "--type",
                "note",
                "--title",
                f"Note {index}",
                "--text",
                "Synthetic body.",
            )
        self.one_view('type: "note"', "limit: 50")
        self.assertEqual(self.accept("view", "run", "probe")["total"], 3)
        narrowed = self.accept("view", "run", "probe", "--limit", "1")
        self.assertEqual(narrowed["total"], 3)
        self.assertEqual(len(narrowed["items"]), 1)
        self.assertTrue(narrowed["has_more"])
        second = self.accept("view", "run", "probe", "--limit", "1", "--offset", "1")
        self.assertNotEqual(
            second["items"][0]["id"], narrowed["items"][0]["id"]
        )


if __name__ == "__main__":
    unittest.main()

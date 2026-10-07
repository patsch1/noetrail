from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from noetrail.body import DEFAULT_BODY_HEADINGS, read_body_headings
from noetrail.commands.review import bookmark_has_personal_note
from noetrail.errors import ConfigurationError
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class BodyHeadingConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, text: str) -> Path:
        path = self.base / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_shipped_defaults_are_english(self) -> None:
        self.assertEqual(DEFAULT_BODY_HEADINGS["personal_note"], "Personal note")
        shipped = read_body_headings(REPOSITORY_ROOT / ".knowledge" / "config.yaml")
        self.assertEqual(shipped, DEFAULT_BODY_HEADINGS)

    def test_instance_configuration_can_restore_german_headings(self) -> None:
        path = self.write_config(
            'version: 9\n'
            'body_headings:\n'
            '  personal_note: "Persönliche Notiz"\n'
            '  ingredients: "Zutaten"\n'
            'statuses:\n'
            '  - "active"\n'
        )
        self.assertEqual(
            read_body_headings(path),
            {"personal_note": "Persönliche Notiz", "ingredients": "Zutaten"},
        )

    def test_unknown_heading_key_is_rejected(self) -> None:
        path = self.write_config('body_headings:\n  invented: "Nope"\n')
        with self.assertRaises(ConfigurationError):
            read_body_headings(path)

    def test_missing_file_falls_back_to_defaults(self) -> None:
        self.assertEqual(read_body_headings(self.base / "absent.yaml"), {})

    def test_legacy_german_note_is_still_detected(self) -> None:
        body = (
            "## Link\n\n[x](https://example.org)\n\n"
            "## Persönliche Notiz\n\nWichtig.\n"
        )
        self.assertTrue(bookmark_has_personal_note(body))
        self.assertTrue(
            bookmark_has_personal_note(body, {"personal_note": "Personal note"})
        )

    def test_english_note_is_detected(self) -> None:
        body = "## Personal note\n\nWhy I saved this.\n"
        self.assertTrue(bookmark_has_personal_note(body))

    def test_empty_note_section_does_not_count(self) -> None:
        body = "## Personal note\n\n## Generated summary\n\nText.\n"
        self.assertFalse(bookmark_has_personal_note(body))


class BookmarkBodyLanguageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.data.mkdir()
        self.config.mkdir()
        self.assertEqual(self.run_cli("init").returncode, 0)

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
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def save_bookmark(self) -> Path:
        payload = self.base / "bookmark.json"
        payload.write_text(
            json.dumps(
                {
                    "title": "An article",
                    "note": "Why this matters to me.",
                    "summary": "A neutral description.",
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "bookmark",
            "https://example.org/article",
            "--metadata-file",
            str(payload),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.data / json.loads(result.stdout)["created"]

    def test_new_bookmarks_use_the_shipped_english_headings(self) -> None:
        content = self.save_bookmark().read_text(encoding="utf-8")
        self.assertIn("## Personal note", content)
        self.assertIn("## Generated summary", content)
        self.assertIn("[Open the original page]", content)

    def test_instance_override_changes_new_bookmark_headings(self) -> None:
        (self.config / "config.yaml").write_text(
            'body_headings:\n'
            '  personal_note: "Persönliche Notiz"\n'
            '  generated_summary: "Automatisch erzeugte Zusammenfassung"\n',
            encoding="utf-8",
        )
        content = self.save_bookmark().read_text(encoding="utf-8")
        self.assertIn("## Persönliche Notiz", content)
        self.assertIn("## Automatisch erzeugte Zusammenfassung", content)
        self.assertNotIn("## Personal note", content)

        review = json.loads(self.run_cli("review").stdout)
        reasons = [
            reason for item in review["items"] for reason in item["reasons"]
        ]
        self.assertNotIn("bookmark_missing_personal_note", reasons)


if __name__ == "__main__":
    unittest.main()

"""A type filter that matches nothing must not read as an empty vault.

This exists because of a real answer given to a real user. Asked which recipes
were saved, an agent searched `type: recipe`, got `"total": 0`, and replied
that there were none. Five recipes were in the vault as notes -- captured
before the `recipe` type existed, which is a window every future type will
have too.

The search response now carries what lifting the type restriction would find,
so the difference between "no entry of this type" and "no such entry" is in
the data rather than left to whoever reads it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest

from tests import CLI_COMMAND, temporary_root


class AbsentTypeSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.run_cli("init")
        # The reported vault in miniature: recipes filed as notes, plus one
        # note that is genuinely just a note.
        self.capture(
            "note", "Spinat-Curry mit Räuchertofu", "Ein Rezept für 3 Portionen"
        )
        self.capture("note", "Bewährter Pizzateig", "Ein Rezept mit Tipo 00")
        self.capture("note", "Zamioculcas", "Zimmerpflanze für wenig Licht")
        self.run_cli(
            "capture",
            "--type",
            "product",
            "--product-kind",
            "rum",
            "--title",
            "Rum aus Barbados",
            "--text",
            "Ein Rezept steht hier nicht drin",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def capture(self, entry_type: str, title: str, text: str) -> None:
        self.run_cli("capture", "--type", entry_type, "--title", title, "--text", text)

    def search(self, *arguments: str) -> dict[str, object]:
        return json.loads(self.run_cli("search", *arguments).stdout)

    def test_the_reported_case_reports_what_exists_instead(self) -> None:
        result = self.search("--type", "recipe", "")
        self.assertEqual(result["total"], 0)
        self.assertEqual(
            result["without_type_filter"],
            {"total": 4, "by_type": {"note": 3, "product": 1}},
        )

    def test_the_text_query_still_applies_when_the_type_is_lifted(self) -> None:
        # Not "everything in the vault": the counterfactual keeps the query and
        # drops only the type restriction, so an answer built from it stays
        # about what the user asked for.
        result = self.search("--type", "recipe", "Rezept")
        self.assertEqual(result["total"], 0)
        self.assertEqual(
            result["without_type_filter"],
            {"total": 3, "by_type": {"note": 2, "product": 1}},
        )

    def test_a_vault_with_genuinely_no_match_says_so(self) -> None:
        # Nothing matches with or without the type, so there is nothing to
        # report and the key stays absent. "No such entry" is then the honest
        # answer, and the response does not dress it up.
        result = self.search("--type", "recipe", "Zabaione")
        self.assertEqual(result["total"], 0)
        self.assertNotIn("without_type_filter", result)

    def test_a_search_that_finds_something_is_unchanged(self) -> None:
        result = self.search("--type", "note", "Rezept")
        self.assertEqual(result["total"], 2)
        self.assertNotIn("without_type_filter", result)

    def test_a_search_without_a_type_filter_is_unchanged(self) -> None:
        empty = self.search("Zabaione")
        self.assertEqual(empty["total"], 0)
        self.assertNotIn("without_type_filter", empty)
        found = self.search("Rezept")
        self.assertEqual(found["total"], 3)
        self.assertNotIn("without_type_filter", found)

    def test_bm25_ranking_reports_the_same_counterfactual(self) -> None:
        # The two ranking modes must not disagree about what exists; only
        # about ordering. Run without an index, which is the default.
        result = self.search("--rank", "bm25", "--type", "recipe", "Rezept")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["without_type_filter"]["total"], 3)


if __name__ == "__main__":
    unittest.main()

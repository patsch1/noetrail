"""The default ranking mode, and why it is the union of the other two.

Neither single mode is a superset of the other, and the repository measured
how much that costs: on `tools/retrieval_set.json`, a literal scan returns
nothing at all for 14 of 20 queries, while a tokenised ranking misses `hydrat`
against "hydration" and `noodle` against "noodles". Those are not
implementation defects. They are what matching a literal substring means once
a query has more than one word, and what tokenising means once a query is a
prefix or a singular.

Hybrid does not add a third way of matching. It runs both and unions the
result, which is why the tests below are about membership and order rather
than about scores: every ranked match must survive, every literal match a
ranking could not produce must survive, an entry both routes found must appear
once, and a ranked match must sort above a merely literal one.
"""

from __future__ import annotations

import unittest

from noetrail.errors import InvalidRequest
from tests.test_search_index import VaultHarness


class HybridRankTest(VaultHarness, unittest.TestCase):
    def test_nonempty_queries_without_tokens_do_not_list_the_vault(self) -> None:
        self.capture("Unrelated", "A synthetic tea note.")
        for query in ("☕", "!!!", "x" * 65, "   "):
            matching = self.capture(f"Literal {query!r}", f"Marker: {query}")
            for indexed in (False, True):
                if indexed:
                    self.json_cli("index", "rebuild")
                with self.subTest(query=query, indexed=indexed):
                    self.assertEqual(
                        self.identifiers(self.search("--", query)), [matching]
                    )
                    self.assertEqual(
                        self.search("--rank", "bm25", "--", query)["total"], 0
                    )
                    self.assertEqual(self.search("--", "absent☕")["total"], 0)
            self.json_cli("index", "drop")

    def word_forms(self) -> dict[str, str]:
        """Two entries a single mode each fails to find.

        `grinder` holds "grind" as a substring but tokenises to a different
        term, so a ranking cannot return it for the query `grind`. `pour over
        method` shares no literal run with the query "method for pouring", so
        a literal scan cannot return it.
        """

        return {
            "grinder": self.capture(
                "Burr grinder",
                "The grinder holds its setting between bags.",
            ),
            "grind": self.capture(
                "Grind size",
                "A finer grind raises extraction in the basket.",
            ),
            "pour": self.capture(
                "Pour over method",
                "Bloom first, then pour in slow concentric circles.",
            ),
        }

    def identifiers(self, response: dict) -> list[str]:
        return [str(item["id"]) for item in response["items"]]

    def test_hybrid_is_what_a_caller_gets_without_asking(self) -> None:
        self.seed()
        default = self.search("--", "bitter espresso")
        explicit = self.search("--rank", "hybrid", "--", "bitter espresso")
        self.assertEqual(default, explicit)

    def test_it_finds_what_a_tokenised_ranking_cannot(self) -> None:
        entries = self.word_forms()
        ranked = self.search("--rank", "bm25", "--", "grind")
        self.assertNotIn(entries["grinder"], self.identifiers(ranked))
        hybrid = self.search("--rank", "hybrid", "--", "grind")
        self.assertIn(entries["grinder"], self.identifiers(hybrid))

    def test_it_finds_what_a_literal_scan_cannot(self) -> None:
        entries = self.word_forms()
        literal = self.search("--rank", "substring", "--", "method for pouring")
        self.assertEqual(literal["total"], 0)
        hybrid = self.search("--rank", "hybrid", "--", "method for pouring")
        self.assertIn(entries["pour"], self.identifiers(hybrid))

    def test_a_ranked_match_sorts_above_a_merely_literal_one(self) -> None:
        # Both entries answer `grind`, but only one of them contains the term.
        # The literal hit is worth returning and is not worth returning first:
        # a substring can fall inside a word that means something else.
        entries = self.word_forms()
        found = self.identifiers(self.search("--rank", "hybrid", "--", "grind"))
        self.assertIn(entries["grind"], found)
        self.assertIn(entries["grinder"], found)
        self.assertLess(
            found.index(entries["grind"]),
            found.index(entries["grinder"]),
        )

    def test_an_entry_both_routes_return_appears_once(self) -> None:
        entries = self.word_forms()
        found = self.identifiers(self.search("--rank", "hybrid", "--", "grind"))
        self.assertEqual(found.count(entries["grind"]), 1)
        self.assertEqual(len(found), len(set(found)))
        self.assertEqual(
            self.search("--rank", "hybrid", "--", "grind")["total"], len(found)
        )

    def test_the_indexed_answer_equals_the_scanned_answer(self) -> None:
        """Same claim the BM25 routes are held to, for the merged one.

        The literal half never consults the index, so a disagreement here
        would mean the merge itself depends on whether the cache existed.
        """

        self.seed()
        self.word_forms()
        cases: list[tuple[str, tuple[str, ...]]] = [
            ("grind", ()),
            ("bitter", ()),
            ("method for pouring", ()),
            ("grind", ("--sort", "title_asc")),
            ("grind", ("--limit", "1")),
            ("grind", ("--limit", "1", "--offset", "1")),
            ("nothing-matches-this", ()),
            ("", ()),
        ]

        def ask(query: str, options: tuple[str, ...]) -> dict:
            return self.search("--rank", "hybrid", *options, "--", query)

        self.json_cli("index", "rebuild")
        with_index = [ask(query, options) for query, options in cases]
        self.json_cli("index", "drop")
        without_index = [ask(query, options) for query, options in cases]
        for case, indexed, scanned in zip(
            cases, with_index, without_index, strict=True
        ):
            self.assertEqual(indexed, scanned, case)

    def test_an_empty_query_still_lists_everything_once(self) -> None:
        entries = self.word_forms()
        listed = self.search("--rank", "hybrid", "--", "")
        self.assertEqual(listed["total"], len(entries))
        self.assertEqual(len(set(self.identifiers(listed))), len(entries))

    def test_relevance_ordering_is_available_and_literal_ranking_refuses_it(
        self,
    ) -> None:
        self.word_forms()
        self.assertEqual(
            self.run_cli(
                "search", "--rank", "hybrid", "--sort", "relevance", "--", "grind"
            ),
            0,
        )
        with self.assertRaises(InvalidRequest):
            self.run_cli(
                "search",
                "--rank",
                "substring",
                "--sort",
                "relevance",
                "--",
                "grind",
            )


if __name__ == "__main__":
    unittest.main()

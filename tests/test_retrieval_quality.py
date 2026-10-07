"""The synthetic retrieval set, and what it is allowed to claim.

`tools/retrieval_set.json` is written by hand in this repository. That makes it
useful as a regression guard and useless as a competitive number, and both
halves need enforcing: the structural tests keep a judgement from quietly
becoming unreachable, and the fairness test keeps the query list from drifting
into a set that only contains cases BM25 wins.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from tools.measure_retrieval import CUTOFFS

from noetrail.search import tokenize

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL_SET = REPOSITORY_ROOT / "tools" / "retrieval_set.json"


def load() -> dict:
    return json.loads(RETRIEVAL_SET.read_text(encoding="utf-8"))


class RetrievalSetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.definition = load()
        self.entries = {
            str(entry["key"]): entry for entry in self.definition["entries"]
        }

    def entry_tokens(self, key: str) -> set[str]:
        entry = self.entries[key]
        return set(
            tokenize(
                " ".join(
                    [
                        str(entry["title"]),
                        str(entry["text"]),
                        " ".join(str(tag) for tag in entry.get("tags", [])),
                    ]
                )
            )
        )

    def test_the_set_is_structurally_sound(self) -> None:
        self.assertEqual(
            len(self.entries), len(self.definition["entries"]), "duplicate key"
        )
        self.assertGreaterEqual(len(self.entries), 30)
        self.assertGreaterEqual(len(self.definition["queries"]), 15)
        for query in self.definition["queries"]:
            self.assertTrue(query["relevant"], query["query"])
            for key in query["relevant"]:
                self.assertIn(key, self.entries, query["query"])

    def test_it_says_in_the_file_that_it_is_not_an_external_benchmark(
        self,
    ) -> None:
        """The caveat has to travel with the data, not only with the report."""

        about = " ".join(self.definition["about"])
        self.assertIn("NOT an external", about)
        for named in ("LoCoMo", "LongMemEval", "BEAM"):
            self.assertIn(named, about)

    def test_every_judgement_is_reachable_by_at_least_one_engine(self) -> None:
        """A judgement no engine can satisfy measures nothing.

        Either the query occurs as a literal substring of the entry, or the
        entry shares a token with it. Without this, adding a plausible-sounding
        query with no lexical anchor would lower both scores and look like a
        finding.
        """

        for query in self.definition["queries"]:
            terms = set(tokenize(str(query["query"])))
            for key in query["relevant"]:
                entry = self.entries[key]
                haystack = f"{entry['title']} {entry['text']}".casefold()
                reachable = bool(terms & self.entry_tokens(key)) or (
                    str(query["query"]).casefold() in haystack
                )
                self.assertTrue(reachable, f"{query['query']} -> {key}")

    def test_the_set_keeps_queries_the_substring_engine_wins(self) -> None:
        """Otherwise the comparison measures the author, not the engine.

        A query whose terms appear in no relevant entry's token set cannot be
        answered by BM25 at all, but can be answered by substring matching --
        a prefix of a longer word, or a singular where the text has a plural.
        At least two such cases stay in the set on purpose.
        """

        unreachable_for_bm25 = [
            query["query"]
            for query in self.definition["queries"]
            if all(
                not (set(tokenize(str(query["query"]))) & self.entry_tokens(key))
                for key in query["relevant"]
            )
        ]
        self.assertGreaterEqual(len(unreachable_for_bm25), 2, unreachable_for_bm25)


class RetrievalComparisonTest(unittest.TestCase):
    """Runs the measurement, so a tokenizer change cannot silently regress it.

    Only the direction is asserted. The published figures live in
    `docs/limits.md`, where they carry the machine they were taken on; pinning
    them here would make an unrelated wording change to an entry fail the
    suite.
    """

    def test_bm25_retrieves_better_than_substring_on_this_set(self) -> None:
        import tempfile

        from tools.measure_retrieval import build_vault, evaluate, run

        definition = load()
        with tempfile.TemporaryDirectory(prefix="noetrail-retrieval-test-") as name:
            base = Path(name)
            data = base / "data"
            config = base / "config"
            data.mkdir()
            config.mkdir()
            identifiers = build_vault(data, config, definition["entries"])
            run(data, config, "index", "rebuild")
            modes = {
                rank: evaluate(
                    data, config, definition["queries"], identifiers, rank
                )
                for rank in ("substring", "bm25")
            }

        for cutoff in CUTOFFS:
            self.assertGreater(
                modes["bm25"]["recall_at"][str(cutoff)],
                modes["substring"]["recall_at"][str(cutoff)],
                f"recall@{cutoff}",
            )
        self.assertGreater(modes["bm25"]["mrr"], modes["substring"]["mrr"])
        # And BM25 must not be winning by returning everything: a query with a
        # rare term still has to come back narrow.
        rare = next(
            item
            for item in modes["bm25"]["per_query"]
            if item["query"] == "tubeless"
        )
        self.assertEqual(rare["total_matches"], 1)


if __name__ == "__main__":
    unittest.main()

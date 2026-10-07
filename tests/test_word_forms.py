"""Conservative candidates must not turn shared fragments into evidence."""

import unittest

from noetrail.index import index_path
from noetrail.mcp import NoetrailServer
from noetrail.search import word_form_terms
from tests.test_search_index import VaultHarness


class WordFormRulesTest(unittest.TestCase):
    def test_endings_are_bounded_and_not_general_fuzzy_matching(self):
        for query, text in (("Kameras", "Kamera"), ("bitter", "bittere"),
                            ("reports", "report"), ("Ketten", "Kette")):
            with self.subTest(query=query):
                self.assertTrue(word_form_terms(query, text))
        for query, text in (("reporter", "report"), ("running", "run"),
                            ("cats", "cat"), ("12345s", "12345"),
                            ("☕", "Kaffee"), ("", "Kamera"),
                            ("kette" * 14, "Kette")):
            with self.subTest(query=query):
                self.assertFalse(word_form_terms(query, text))
        self.assertFalse(word_form_terms(
            " ".join(f"token{n}" for n in range(17)) + " Kameras", "Kamera"
        ))

    def test_compounds_need_both_parts_and_a_whole_word_anchor(self):
        self.assertEqual(
            word_form_terms("Fahrradkette", "Fahrradwartung. Die Kette ölen."),
            ("fahrradwartung", "kette"),
        )
        self.assertTrue(word_form_terms("Gartenschere", "Gartenarbeiten Schere"))
        for query, text in (
            ("Bernsteinkette", "Fahrradwartung Kette"),
            ("Winterjacke", "Sommerreise Jacke"),
            ("Gartenschere", "Gartenarbeiten Scherenpflege"),
            ("Kettekette", "Kette"),
        ):
            with self.subTest(query=query, text=text):
                self.assertFalse(word_form_terms(query, text))


class WordFormSearchTest(VaultHarness, unittest.TestCase):
    def test_compound_cache_parity_and_existing_hits_remain_unchanged(self):
        wanted = self.capture("Fahrradwartung", "Die Kette trocknen und ölen.")
        self.capture("Andere Tätigkeit", "Eine Jacke einpacken.")
        query = "Wie pflege ich meine Fahrradkette?"
        before = self.search(query)
        self.assertEqual([r["id"] for r in before["items"]], [wanted])
        self.assertEqual(before["items"][0]["match_kind"], "word_form")
        self.assertNotIn("match_evidence", before["items"][0])
        for rank in ("bm25", "substring"):
            self.assertEqual(self.search("--rank", rank, query)["total"], 0)
        self.json_cli("index", "rebuild")
        self.assertEqual(self.search(query), before)
        index_path(self.layout()).write_bytes(b"corrupt test cache")
        self.assertEqual(self.search(query), before)
        exact = self.capture("Fahrradkette", "Ein exakter Treffer.")
        self.assertEqual(self.search(query)["total"], 1)
        self.assertEqual(self.search(query)["items"][0]["id"], exact)
        self.assertNotIn("match_kind", self.search(query)["items"][0])
        self.assertEqual(self.search("--offset", "1", query)["items"], [])

    def test_filters_and_pagination_apply_before_returning_fallbacks(self):
        ids = [self.capture(f"Gartenarbeiten {n}", "Die Schere reinigen.")
               for n in range(3)]
        first = self.search("--limit", "2", "Gartenschere")
        second = self.search("--limit", "2", "--offset", "2", "Gartenschere")
        self.assertEqual(first["total"], 3)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(
            [r["id"] for r in first["items"] + second["items"]], sorted(ids)
        )
        restricted = self.search("--type", "project", "Gartenschere")
        self.assertEqual(restricted["total"], 0)
        self.assertEqual(restricted["without_type_filter"]["by_type"], {"note": 3})
        filtered = self.search("--type", "project", "--domain", "example.org",
                               "Gartenschere")
        self.assertEqual(filtered["total"], 0)
        self.assertNotIn("without_type_filter", filtered)
        # An exact hit excluded by the type filter must not suppress fallback.
        self.capture("Gartenschere", "Exaktes Projekt.", type="project")
        self.assertEqual(self.search("--type", "note", "Gartenschere")["total"], 3)
        # The two anchors cannot be collected from unrelated entries.
        self.capture("Winterreise", "Keine Ausrüstung verzeichnet.")
        self.capture("Sommerreise", "Eine Jacke einpacken.")
        self.assertEqual(self.search("Winterjacke")["total"], 0)
        self.assertEqual(self.search("Bernsteinkette")["total"], 0)

    def test_search_retrieve_and_mcp_preserve_label_and_evidence_provenance(self):
        wanted = self.capture(
            "Fahrradwartung",
            "<!-- noetrail:web-content -->\nDie Kette trocknen.\n"
            "<!-- /noetrail:web-content -->",
        )
        response = self.search("--explain", "Fahrradkette")
        evidence = response["items"][0]["match_evidence"]
        self.assertTrue(any(e["origin"] == "web" and "Kette" in e["excerpt"]
                            for e in evidence))
        server = NoetrailServer(self.layout())
        self.assertEqual(server.call_tool(
            "search", {"query": "Fahrradkette", "explain": True}
        ), response)
        retrieved = server.call_tool("retrieve", {"query": "Fahrradkette"})
        self.assertEqual(retrieved["items"][0]["id"], wanted)
        self.assertEqual(retrieved["items"][0]["match_kind"], "word_form")
        self.assertIn("Kette", retrieved["items"][0]["body"])

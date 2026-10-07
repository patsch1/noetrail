"""Explanations are bounded, opt-in and retain fetched-text provenance."""

import unittest

from noetrail.mcp import NoetrailServer
from tests.test_search_index import VaultHarness


class SearchEvidenceTest(VaultHarness, unittest.TestCase):
    def test_unicode_normalization_keeps_the_actual_match_in_the_excerpt(self):
        self.capture("Compatibility forms", "ß" * 300 + " ＮＯＲＴＨ here")
        evidence = self.search("--explain", "north")["items"][0]["match_evidence"]
        self.assertIn("ＮＯＲＴＨ", evidence[0]["excerpt"])

    def test_excerpts_preserve_origin_and_do_not_expand_default_results(self):
        self.capture(
            "Synthetic note",
            "Personal observation.\n<!-- noetrail:web-content -->\n"
            "Fetched needle " + "x" * 400 + "\n<!-- /noetrail:web-content -->",
        )
        plain = self.search("needle")["items"][0]
        self.assertNotIn("match_evidence", plain)
        explained = self.search("--explain", "needle")["items"][0]
        evidence = explained["match_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["origin"], "web")
        self.assertEqual(evidence[0]["field"], "body")
        self.assertIn("needle", evidence[0]["excerpt"])
        self.assertLessEqual(len(evidence[0]["excerpt"]), 240)
        self.assertTrue(evidence[0]["truncated"])
        server = NoetrailServer(self.layout())
        self.assertEqual(
            server.call_tool("search", {"query": "needle", "explain": True})["items"][
                0
            ]["match_evidence"],
            evidence,
        )
        self.json_cli("index", "rebuild")
        self.assertEqual(self.search("--explain", "needle")["items"][0], explained)

    def test_excerpt_count_is_bounded_and_empty_query_has_no_evidence(self):
        self.capture("needle", "\n".join(f"## needle {n}" for n in range(10)))
        self.assertEqual(
            len(self.search("--explain", "needle")["items"][0]["match_evidence"]), 5
        )
        self.assertEqual(self.search("--explain", "")["items"][0]["match_evidence"], [])

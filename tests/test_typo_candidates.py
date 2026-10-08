"""Typos produce labelled hints, never fuzzy body evidence or saved aliases."""

import json
from pathlib import Path
import tempfile
import unittest

from tools.measure_questions import build, evaluate

from noetrail.index import index_path
from noetrail.mcp import NoetrailServer
from noetrail.search import one_edit_apart, typo_terms
from tests.test_search_index import VaultHarness


class TypoRulesTest(unittest.TestCase):
    def test_transfer_distractors_stay_empty_without_retuning_the_rules(self):
        definition = json.loads(
            (Path(__file__).resolve().parents[1] / "tools/typo_set.json").read_text()
        )
        with tempfile.TemporaryDirectory(prefix="noetrail-typo-transfer-") as name:
            base = Path(name)
            data, config = base / "data", base / "config"
            data.mkdir()
            config.mkdir()
            identifiers = build(data, config, definition)
            result = evaluate(data, config, definition, identifiers, "hybrid")
        self.assertEqual(result["recall_at_5"], 1.0)
        self.assertEqual(result["correct_empty_rate"], 1.0)
        self.assertEqual(result["irrelevant_results"], 0)

    def test_single_edit_operations_and_two_edit_rejections(self):
        for a, b in (
            ("espresso", "espreso"),
            ("kamera", "kamerra"),
            ("laterne", "laterme"),
            ("bewässerung", "bewässerugn"),
        ):
            with self.subTest(a=a, b=b):
                self.assertTrue(one_edit_apart(a, b))
                self.assertTrue(one_edit_apart(b, a))
        for a, b in (
            ("laterne", "laterne"),
            ("laterne", "lamarme"),
            ("kamera", "ramake"),
            ("laterne", "lat"),
        ):
            self.assertFalse(one_edit_apart(a, b))

    def test_only_bounded_alphabetic_title_and_alias_tokens_are_eligible(self):
        self.assertEqual(typo_terms("Kamerra", {"title": "ＫＡＭＥＲＡ"}), ("kamera",))
        self.assertEqual(
            typo_terms("Northerm", {"title": "Station", "aliases": ["Northern"]}),
            ("northern",),
        )
        for query, title in (
            ("abc", "abd"),
            ("123456", "123457"),
            ("abcdefab", "abcdefac"),
            ("a" * 65, "a" * 64),
        ):
            self.assertFalse(typo_terms(query, {"title": title}))
        self.assertTrue(
            typo_terms(" ".join(["word"] * 16 + ["Kamerra"]), {"title": "Kamera"})
        )
        self.assertFalse(
            typo_terms(
                " ".join(f"word{n}" for n in range(16)) + " Kamerra",
                {"title": "Kamera"},
            )
        )


class TypoSearchTest(VaultHarness, unittest.TestCase):
    def test_empty_hybrid_fallback_cache_parity_and_explicit_modes(self):
        wanted = self.capture("Balkonbewässerung", "Pflanzen morgens gießen.")
        self.capture("Other topic", "Farbe prüfen.")
        before = self.search("--explain", "Balkonbewässerugn")
        item = before["items"][0]
        self.assertEqual(item["id"], wanted)
        self.assertEqual(item["match_kind"], "typo")
        self.assertEqual(item["matched_terms"], ["balkonbewässerung"])
        self.assertEqual(item["match_evidence"][0]["field"], "title")
        self.assertIn("Balkonbewässerung", item["match_evidence"][0]["excerpt"])
        for rank in ("bm25", "substring"):
            self.assertEqual(
                self.search("--rank", rank, "Balkonbewässerugn")["total"], 0
            )
        self.json_cli("index", "rebuild")
        self.assertEqual(self.search("--explain", "Balkonbewässerugn"), before)
        index_path(self.layout()).write_bytes(b"broken synthetic index")
        self.assertEqual(self.search("--explain", "Balkonbewässerugn"), before)
        server = NoetrailServer(self.layout())
        self.assertEqual(
            server.call_tool("search", {"query": "Balkonbewässerugn", "explain": True}),
            before,
        )
        retrieved = server.call_tool("retrieve", {"query": "Balkonbewässerugn"})
        self.assertEqual(retrieved["items"][0]["match_kind"], "typo")
        self.assertEqual(retrieved["items"][0]["matched_terms"], item["matched_terms"])

    def test_alias_provenance_filters_and_literal_precedence(self):
        wanted = self.capture("Station", "Synthetic record.", alias="Northern")
        self.capture("Body only", "A distant southern example.")
        self.assertEqual(self.search("southetn")["total"], 0)
        hinted = self.search("--explain", "Northerm")["items"][0]
        self.assertEqual(hinted["id"], wanted)
        self.assertEqual(hinted["match_evidence"][0]["field"], "aliases")
        filtered = self.search("--type", "project", "Northerm")
        self.assertEqual(filtered["total"], 0)
        self.assertEqual(filtered["without_type_filter"]["by_type"], {"note": 1})
        exact = self.capture("Northerm", "Exact spelling.", type="project")
        self.assertEqual([r["id"] for r in self.search("Northerm")["items"]], [exact])
        self.assertNotIn("match_kind", self.search("Northerm")["items"][0])
        self.assertEqual(
            self.search("--type", "note", "Northerm")["items"][0]["id"], wanted
        )
        self.assertEqual(
            self.search("--type", "note", "--domain", "example.org", "Northerm")[
                "total"
            ],
            0,
        )

    def test_tied_candidates_have_stable_pages_and_do_not_change_entries(self):
        ids = [self.capture(f"Laterne {n}", "Synthetic light.") for n in range(3)]
        before = {p: p.read_bytes() for p in self.data.rglob("*.md")}
        first = self.search("--limit", "2", "Laterme")
        second = self.search("--limit", "2", "--offset", "2", "Laterme")
        self.assertEqual(
            [r["id"] for r in first["items"] + second["items"]], sorted(ids)
        )
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual({p: p.read_bytes() for p in self.data.rglob("*.md")}, before)

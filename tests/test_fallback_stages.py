"""Body typos and compound components: the last two empty-result stages."""

import unittest
from unittest.mock import patch

from noetrail.deadline import ReadTimeout, read_deadline
from noetrail.index import index_path
from noetrail.mcp import NoetrailServer
from noetrail.search import body_typo_terms, component_terms
from tests.test_search_index import VaultHarness


class BodyTypoRulesTest(unittest.TestCase):
    def test_one_edit_from_a_name_like_body_word(self):
        self.assertEqual(
            body_typo_terms("Mondarchvi", "Der Roman Mondarchiv steht hier."),
            ("mondarchiv",),
        )
        self.assertEqual(
            body_typo_terms("Morgenstren", "Im Projekt Morgenstern, danach."),
            ("morgenstern",),
        )
        # Equal tokens are not typos: the lexical stages own them.
        self.assertFalse(body_typo_terms("Mondarchiv", "Der Roman Mondarchiv."))

    def test_minimum_length_is_seven_letters(self):
        self.assertEqual(
            body_typo_terms("Kanzlai", "Die Kanzlei öffnet."), ("kanzlei",)
        )
        self.assertFalse(body_typo_terms("Mandel", "Der Mantel hängt."))
        self.assertFalse(body_typo_terms("Mantel", "Der Mantell hängt."))

    def test_any_body_word_counts_regardless_of_case_or_position(self):
        for body in (
            "Dampfventil kontrollieren.",
            "Achtung: Dampfventil kontrollieren.",
            "- Dampfventil kontrollieren",
            "Das dampfventil kontrollieren.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    body_typo_terms("Dampfventli", body), ("dampfventil",)
                )
        # Only the body: the provenance fence is machinery, not a word.
        self.assertFalse(
            body_typo_terms(
                "noetrall", "<!-- noetrail:web-content -->\nText\n"
                "<!-- /noetrail:web-content -->",
            )
        )

    def test_hash_like_numeric_and_oversized_words_are_excluded(self):
        for query, body in (
            ("abcdefabce", "Der Wert Abcdefabcd steht."),
            ("Kanzlei12", "Die Kanzlei13 öffnet."),
            ("K" + "a" * 64, "Die K" + "a" * 63 + " öffnet."),
        ):
            with self.subTest(query=query):
                self.assertFalse(body_typo_terms(query, body))

    def test_at_most_two_terms_and_the_sixteen_token_cap(self):
        body = "Kanzlei, Laterne und Mondarchiv stehen."
        self.assertEqual(
            body_typo_terms("Kanzlai Latarne Mondarchvi", body),
            ("kanzlei", "laterne"),
        )
        self.assertTrue(
            body_typo_terms(" ".join(["wort"] * 16 + ["Kanzlai"]), body)
        )
        self.assertFalse(
            body_typo_terms(
                " ".join(f"wort{n}" for n in range(16)) + " Kanzlai", body
            )
        )

    def test_both_scans_check_the_shared_deadline(self):
        clock = [0.0]

        def tick() -> float:
            clock[0] += 1.0
            return clock[0]

        for scan in (body_typo_terms, component_terms):
            with self.subTest(scan=scan.__name__):
                with (
                    patch("noetrail.deadline.time.monotonic", side_effect=tick),
                    self.assertRaises(ReadTimeout),
                    read_deadline(5),
                ):
                    scan(
                        "Mondarchvi Zugreise",
                        " ".join("abwasserkanal" + "x" * n for n in range(10)),
                    )


class ComponentRulesTest(unittest.TestCase):
    def test_a_component_opens_or_closes_a_longer_observed_word(self):
        self.assertEqual(
            component_terms("Zugreise schlafen", "Nachtzugreise im Liegewagen"),
            ("nachtzugreise",),
        )
        self.assertEqual(
            component_terms("Balkon gießen", "Balkonbewässerung"),
            ("balkonbewässerung",),
        )
        # Never an arbitrary infix.
        self.assertFalse(component_terms("eichen Papier", "Zeichenblock"))

    def test_component_and_extension_bounds(self):
        self.assertFalse(component_terms("Jacke Reißverschluss", "Regenjacke"))
        for observed, expected in (
            ("Zugreisen", False),
            ("Zugreisend", False),
            ("Zugreisende", True),
        ):
            with self.subTest(observed=observed):
                self.assertEqual(
                    bool(component_terms("Zugreise", observed)), expected
                )
        self.assertFalse(component_terms("Zugreise", "Zugreise"))

    def test_hash_like_numeric_cap_and_token_limit(self):
        self.assertFalse(component_terms("abcdef", "abcdefabcd"))
        self.assertFalse(component_terms("reise12", "reise12345"))
        self.assertEqual(
            component_terms("Garten", "Gartenarbeit Gartenschere Gartentor"),
            ("gartenarbeit", "gartenschere"),
        )
        self.assertFalse(
            component_terms(
                " ".join(f"wort{n}" for n in range(16)) + " Zugreise",
                "Nachtzugreise",
            )
        )


class FallbackStageSearchTest(VaultHarness, unittest.TestCase):
    def assert_parity(self, *arguments: str) -> dict:
        before = self.search(*arguments)
        self.json_cli("index", "rebuild")
        self.assertEqual(self.search(*arguments), before)
        index_path(self.layout()).write_bytes(b"broken synthetic index")
        self.assertEqual(self.search(*arguments), before)
        index_path(self.layout()).unlink()
        self.assertEqual(self.search(*arguments), before)
        return before

    def test_body_typo_label_evidence_parity_and_explicit_modes(self):
        wanted = self.capture(
            "Leseliste", "Der synthetische Roman Mondarchiv steht hier."
        )
        self.capture("Anderes", "Das Dampfventil prüfen.")
        before = self.assert_parity("--explain", "Mondarchvi")
        self.assertEqual([item["id"] for item in before["items"]], [wanted])
        item = before["items"][0]
        self.assertEqual(item["match_kind"], "typo")
        self.assertEqual(item["matched_terms"], ["mondarchiv"])
        self.assertEqual(item["match_evidence"][0]["field"], "body")
        self.assertIn("Mondarchiv", item["match_evidence"][0]["excerpt"])
        self.assertNotIn("Mondarchvi", item["match_evidence"][0]["excerpt"])
        for rank in ("bm25", "substring"):
            self.assertEqual(self.search("--rank", rank, "Mondarchvi")["total"], 0)
        server = NoetrailServer(self.layout())
        self.assertEqual(
            server.call_tool("search", {"query": "Mondarchvi", "explain": True}),
            before,
        )
        retrieved = server.call_tool("retrieve", {"query": "Mondarchvi"})
        self.assertEqual(retrieved["items"][0]["match_kind"], "typo")
        self.assertEqual(retrieved["items"][0]["matched_terms"], ["mondarchiv"])

    def test_component_label_evidence_parity_and_explicit_modes(self):
        wanted = self.capture("Nachtzugreise", "Ohrstöpsel neben dem Reisepass.")
        self.capture("Anderes", "Eine Zeichenmappe.")
        before = self.assert_parity("--explain", "Zugreise schlafen")
        self.assertEqual([item["id"] for item in before["items"]], [wanted])
        item = before["items"][0]
        self.assertEqual(item["match_kind"], "word_form")
        self.assertEqual(item["matched_terms"], ["nachtzugreise"])
        self.assertEqual(item["match_evidence"][0]["field"], "title")
        self.assertIn("Nachtzugreise", item["match_evidence"][0]["excerpt"])
        for rank in ("bm25", "substring"):
            self.assertEqual(
                self.search("--rank", rank, "Zugreise schlafen")["total"], 0
            )
        server = NoetrailServer(self.layout())
        self.assertEqual(
            server.call_tool(
                "search", {"query": "Zugreise schlafen", "explain": True}
            ),
            before,
        )
        retrieved = server.call_tool("retrieve", {"query": "Zugreise schlafen"})
        self.assertEqual(retrieved["items"][0]["match_kind"], "word_form")
        self.assertEqual(retrieved["items"][0]["matched_terms"], ["nachtzugreise"])

    def test_an_earlier_nonempty_stage_means_later_stages_are_not_consulted(self):
        title = self.capture("Mondarchiv", "Ein Titel.")
        self.capture("Leseliste", "Der Roman Mondarchiv steht hier.")
        self.assertEqual(
            [item["id"] for item in self.assert_parity("Mondarchvi")["items"]],
            [title],
        )
        inflected = self.capture("Zugreisen", "Plural im Titel.")
        self.capture("Nachtzugreise", "Kompositum im Titel.")
        result = self.assert_parity("Zugreise schlafen")
        self.assertEqual([item["id"] for item in result["items"]], [inflected])
        self.assertNotIn("matched_terms", result["items"][0])
        self.assertEqual(result["items"][0]["match_kind"], "word_form")
        # A component outranks a body typo by running first.
        component = self.capture("Wanderrucksack", "Für die Tour.")
        self.capture("Besprechung", "Im Projekt Morgenstern geplant.")
        self.assertEqual(
            self.assert_parity("Morgenstren")["items"][0]["match_kind"], "typo"
        )
        result = self.assert_parity("Rucksack Morgenstren")
        self.assertEqual([item["id"] for item in result["items"]], [component])
        self.assertEqual(result["items"][0]["matched_terms"], ["wanderrucksack"])

    def test_filters_as_of_pagination_and_no_persistence(self):
        source = self.capture("Projektnotiz", "Im Projekt Morgenstern geplant.")
        target = self.capture("Sternwarte", "Ein Ziel.")
        self.json_cli(
            "relate", source, "involves", target,
            "--valid-from", "2024-01-01T00:00:00+00:00",
            "--valid-until", "2025-01-01T00:00:00+00:00",
        )
        relation = ("--related-id", target, "--relation-predicate", "involves")
        held = self.search(*relation, "--as-of", "2024-06-01T00:00:00+00:00",
                           "Morgenstren")
        self.assertEqual([item["id"] for item in held["items"]], [source])
        self.assertEqual(held["items"][0]["match_kind"], "typo")
        lapsed = self.search(*relation, "--as-of", "2025-06-01T00:00:00+00:00",
                             "Morgenstren")
        self.assertEqual(lapsed["total"], 0)
        restricted = self.search("--type", "project", "Morgenstren")
        self.assertEqual(restricted["total"], 0)
        self.assertEqual(restricted["without_type_filter"]["by_type"], {"note": 1})
        ids = [self.capture(f"Reise {n}", "Mit der Nachtzugreise.")
               for n in range(3)]
        before = {path: path.read_bytes() for path in self.data.rglob("*")
                  if path.is_file()}
        first = self.search("--limit", "2", "Zugreise schlafen")
        second = self.search("--limit", "2", "--offset", "2", "Zugreise schlafen")
        self.assertEqual(
            [item["id"] for item in first["items"] + second["items"]], sorted(ids)
        )
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(
            {path: path.read_bytes() for path in self.data.rglob("*")
             if path.is_file()},
            before,
        )

    def test_fused_variants_order_kinds_and_report_observed_terms(self):
        component = self.capture("Nachtzugreise", "Im Liegewagen.")
        title = self.capture("Laterne", "Ein Licht.")
        body = self.capture("Leseliste", "Der Roman Mondarchiv steht hier.")
        result = self.json_cli(
            "retrieve", "Mondarchvi",
            "--query-variant=Laterme",
            "--query-variant=Zugreise schlafen",
        )
        items = {item["id"]: item for item in result["items"]}
        self.assertEqual(
            [item["id"] for item in result["items"]], [component, title, body]
        )
        self.assertEqual(items[component]["match_kind"], "word_form")
        self.assertEqual(items[component]["matched_terms"], ["nachtzugreise"])
        self.assertEqual(items[title]["matched_terms"], ["laterne"])
        self.assertEqual(items[body]["match_kind"], "typo")
        self.assertEqual(items[body]["matched_terms"], ["mondarchiv"])
        self.assertEqual(
            items[body]["query_matches"],
            [{"query": "Mondarchvi", "match_kind": "typo",
              "matched_terms": ["mondarchiv"]}],
        )

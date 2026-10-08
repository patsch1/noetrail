"""Batch retrieval fuses variants under one shared read and output budget."""

import unittest
from unittest.mock import patch

from noetrail.commands.query import _text_matches
from noetrail.errors import InvalidRequest
from noetrail.mcp import NoetrailServer, ToolFailure
from noetrail.store import vault_lock
from tests.test_search_index import VaultHarness


class QueryVariantsTest(VaultHarness, unittest.TestCase):
    def test_all_queries_share_one_deadline_and_release_the_lock_on_timeout(self):
        self.capture("Needle", "Synthetic source.")
        clock = [0.0]

        def slow_match(*args):
            result = _text_matches(*args)
            clock[0] += 12.0
            return result

        server = NoetrailServer(self.layout())
        with (
            patch("noetrail.deadline.time.monotonic", side_effect=lambda: clock[0]),
            patch("noetrail.commands.query._text_matches", side_effect=slow_match),
        ):
            with self.assertRaises(ToolFailure) as failure:
                server.call_tool(
                    "retrieve",
                    {"query": "Needle", "query_variants": ["Unknown", "Other"]},
                )
        self.assertEqual(failure.exception.code, "read_timeout")
        with vault_lock(self.data, exclusive=True):
            pass
        self.assertEqual(server.call_tool("inventory", {})["entry_count"], 1)

    def test_duplicate_ids_in_different_files_are_not_silently_deduplicated(self):
        self.capture("Amber", "Synthetic source.")
        path = next(self.data.rglob("*.md"))
        sibling = path.with_name("synthetic-duplicate.md")
        sibling.write_text(
            path.read_text().replace("Amber", "Cobalt"), encoding="utf-8"
        )
        with self.assertRaisesRegex(InvalidRequest, "Duplicate entry ID"):
            self.json_cli("retrieve", "Amber", "--query-variant=Cobalt")

    def test_translation_and_paraphrase_variants_recover_and_deduplicate_sources(self):
        wanted = self.capture("Teigruhe", "Über Nacht kühl stehen lassen.")
        self.capture("Unrelated topic", "Synthetic label only.")
        args = (
            "retrieve",
            "cold proofing",
            "--query-variant=Teigruhe",
            "--query-variant=kühl stehen",
        )
        result = self.json_cli(*args)
        self.assertEqual(
            result["queries"], ["cold proofing", "Teigruhe", "kühl stehen"]
        )
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["id"], wanted)
        self.assertEqual(item["body"], "Über Nacht kühl stehen lassen.")
        self.assertEqual(
            [m["query"] for m in item["query_matches"]], ["Teigruhe", "kühl stehen"]
        )
        self.assertTrue(
            all(m["match_kind"] == "lexical" for m in item["query_matches"])
        )
        self.json_cli("index", "rebuild")
        self.assertEqual(self.json_cli(*args), result)
        for in_process in (True, False):
            server = NoetrailServer(self.layout(), in_process_reads=in_process)
            self.assertEqual(
                server.call_tool(
                    "retrieve",
                    {
                        "query": "cold proofing",
                        "query_variants": ["Teigruhe", "kühl stehen"],
                    },
                ),
                result,
            )

    def test_fusion_prefers_independent_hits_not_incomparable_bm25_scores(self):
        wanted = self.capture("Shared source", "amber cobalt")
        self.capture("Amber only", "amber " * 20)
        self.capture("Cobalt only", "cobalt " * 20)
        result = self.json_cli(
            "retrieve", "amber", "--query-variant=cobalt", "--limit=1"
        )
        self.assertEqual(result["items"][0]["id"], wanted)
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["returned"], 1)

    def test_candidates_keep_per_query_origin_and_exact_match_wins_over_typo(self):
        wanted = self.capture("Laterne", "Eine Lampe für den Abend.")
        result = self.json_cli("retrieve", "Laterme", "--query-variant=Abend")
        item = result["items"][0]
        self.assertEqual(item["id"], wanted)
        self.assertNotIn("match_kind", item)
        self.assertEqual(
            item["query_matches"],
            [
                {
                    "query": "Laterme",
                    "match_kind": "typo",
                    "matched_terms": ["laterne"],
                },
                {"query": "Abend", "match_kind": "lexical"},
            ],
        )
        typo_only = self.json_cli("retrieve", "Laterme", "--query-variant=unmatched")
        self.assertEqual(typo_only["items"][0]["match_kind"], "typo")
        self.assertEqual(typo_only["items"][0]["matched_terms"], ["laterne"])

    def test_body_and_entry_budgets_are_shared_across_variants(self):
        self.capture("Copper", "a" * 20)
        self.capture("Silver", "b" * 20)
        result = self.json_cli(
            "retrieve",
            "Copper",
            "--query-variant=Silver",
            "--max-body-chars=25",
            "--limit=10",
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["body_chars_returned"], 25)
        self.assertEqual(sum(len(i["body"]) for i in result["items"]), 25)
        self.assertTrue(any(i["body_truncated"] for i in result["items"]))
        for item in result["items"]:
            self.assertIn("revision", item)
            self.assertIn("provenance", item)

    def test_filters_and_type_diagnostic_apply_to_every_variant(self):
        self.capture("Birch", "Synthetic tree.")
        self.capture("Willow", "Synthetic tree.")
        self.capture("Birch project", "Synthetic project.", type="project")
        result = self.json_cli(
            "retrieve", "Birch", "--query-variant=Willow", "--type=note"
        )
        self.assertEqual(result["total"], 2)
        self.assertTrue(all(i["type"] == "note" for i in result["items"]))
        empty = self.json_cli(
            "retrieve", "unmatched", "--query-variant=Willow", "--type=project"
        )
        self.assertEqual(empty["total"], 0)
        self.assertEqual(
            empty["without_type_filter"], {"total": 1, "by_type": {"note": 1}}
        )

    def test_invalid_batches_and_option_like_values_cannot_expand_permissions(self):
        invalid = [
            (["one", "two", "three", "four"], "at most three"),
            (["  "], "nonblank"),
            (["\x00"], "NUL"),
            (["x" * 2001], "2000"),
            (["ＮＥＥＤＬＥ"], "distinct"),
        ]
        for variants, message in invalid:
            with self.subTest(variants=variants[:1]):
                with self.assertRaisesRegex(InvalidRequest, message):
                    self.json_cli(
                        "retrieve",
                        "needle",
                        *("--query-variant=" + q for q in variants),
                    )
                with self.assertRaises(ToolFailure):
                    NoetrailServer(self.layout()).call_tool(
                        "retrieve", {"query": "needle", "query_variants": variants}
                    )
        with self.assertRaisesRegex(InvalidRequest, "nonblank"):
            self.json_cli("retrieve", "", "--query-variant=needle")
        before = {p: p.read_bytes() for p in self.data.rglob("*.md")}
        for in_process in (True, False):
            server = NoetrailServer(self.layout(), in_process_reads=in_process)
            result = server.call_tool(
                "retrieve",
                {"query": "--limit", "query_variants": ["--purge", "--type=person"]},
            )
            self.assertEqual(result["total"], 0)
        self.assertEqual({p: p.read_bytes() for p in self.data.rglob("*.md")}, before)

    def test_single_query_shape_and_empty_inventory_style_retrieval_are_compatible(
        self,
    ):
        self.capture("Needle", "Synthetic source.")
        old = self.json_cli("retrieve", "needle")
        self.assertNotIn("queries", old)
        self.assertNotIn("query_matches", old["items"][0])
        self.assertEqual(
            NoetrailServer(self.layout()).call_tool(
                "retrieve", {"query": "needle", "query_variants": []}
            ),
            old,
        )
        self.assertEqual(self.json_cli("retrieve", "")["total"], 1)

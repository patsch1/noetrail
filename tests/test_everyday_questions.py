"""The diagnostic suite must admit misses and genuinely absent answers."""

import json
import unittest

from tools.measure_questions import DEFAULT_SET, build, evaluate, summarize

from tests.test_search_index import VaultHarness


class EverydayQuestionsTest(VaultHarness, unittest.TestCase):
    def test_diagnostics_include_unreachable_and_unanswerable_questions(self):
        definition = json.loads(DEFAULT_SET.read_text(encoding="utf-8"))
        self.assertEqual(len(definition["queries"]), 100)
        identifiers = build(self.data, self.config, definition)
        report = evaluate(self.data, self.config, definition, identifiers, "hybrid")
        self.assertLess(report["recall_at_5"], 1)
        self.assertGreater(report["recall_at_5"], 0)
        self.assertGreater(report["irrelevant_results"], 0)
        self.assertEqual(report["categories"]["temporal"]["recall_at_5"], 1)
        self.assertEqual(report["categories"]["temporal"]["correct_empty_rate"], 1)
        self.json_cli("index", "rebuild")
        self.assertEqual(
            report, evaluate(self.data, self.config, definition, identifiers, "hybrid")
        )

    def test_empty_answers_do_not_inflate_recall(self):
        report = summarize(
            [
                {
                    "relevant": 1,
                    "recall_at_5": 0,
                    "reciprocal_rank": 0,
                    "returned": 0,
                    "total": 0,
                    "hits": 0,
                },
                {
                    "relevant": 0,
                    "recall_at_5": None,
                    "reciprocal_rank": 0,
                    "returned": 0,
                    "total": 0,
                    "hits": 0,
                },
            ]
        )
        self.assertEqual(report["recall_at_5"], 0)
        self.assertEqual(report["correct_empty_rate"], 1)
        self.assertIsNone(report["precision_at_5"])

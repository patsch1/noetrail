from __future__ import annotations

import json
import unittest

from noetrail.jsonrpc import (
    MAX_JSON_DEPTH,
    JsonRequestError,
    loads_bounded,
    scan_depth,
)


class ScanDepthTest(unittest.TestCase):
    def test_flat_documents_have_depth_one(self) -> None:
        self.assertEqual(scan_depth('{"a": 1}'), 1)
        self.assertEqual(scan_depth("[1, 2, 3]"), 1)

    def test_scalars_have_depth_zero(self) -> None:
        self.assertEqual(scan_depth('"text"'), 0)
        self.assertEqual(scan_depth("42"), 0)

    def test_nesting_is_counted(self) -> None:
        self.assertEqual(scan_depth('{"a": {"b": [1]}}'), 3)

    def test_brackets_inside_strings_are_ignored(self) -> None:
        self.assertEqual(scan_depth('{"a": "[[[[["}'), 1)
        self.assertEqual(scan_depth('{"a": "]]]]]"}'), 1)

    def test_escaped_quotes_do_not_end_a_string(self) -> None:
        self.assertEqual(scan_depth('{"a": "he said \\"[[[\\" once"}'), 1)

    def test_escaped_backslash_before_quote_ends_the_string(self) -> None:
        self.assertEqual(scan_depth('{"a": "back\\\\"}'), 1)


class LoadsBoundedTest(unittest.TestCase):
    def test_valid_documents_round_trip(self) -> None:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(loads_bounded(json.dumps(payload)), payload)

    def test_bytes_are_decoded(self) -> None:
        self.assertEqual(loads_bounded(b'{"a": 1}'), {"a": 1})

    def test_invalid_utf8_is_rejected(self) -> None:
        with self.assertRaises(JsonRequestError):
            loads_bounded(b'{"a": "\xff"}')

    def test_malformed_json_is_rejected(self) -> None:
        with self.assertRaises(JsonRequestError):
            loads_bounded("{not json")

    def test_deeply_nested_documents_are_rejected_before_parsing(self) -> None:
        """The nesting guard must fire instead of `RecursionError`.

        `json.loads` parses containers recursively, so this input used to raise
        `RecursionError`, which the servers did not catch.
        """
        payload = "[" * 5_000 + "]" * 5_000
        with self.assertRaises(JsonRequestError) as raised:
            loads_bounded(payload)
        self.assertIn("nesting", str(raised.exception))

    def test_documents_at_the_limit_are_accepted(self) -> None:
        payload = "[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH
        self.assertIsInstance(loads_bounded(payload), list)

    def test_documents_one_past_the_limit_are_rejected(self) -> None:
        payload = "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
        with self.assertRaises(JsonRequestError):
            loads_bounded(payload)

    def test_a_deep_string_payload_is_not_mistaken_for_nesting(self) -> None:
        payload = json.dumps({"query": "[" * 5_000})
        self.assertEqual(loads_bounded(payload)["query"], "[" * 5_000)  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()

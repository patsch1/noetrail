"""A timed-out read releases its lock and cannot poison the next request."""

import unittest
from unittest.mock import patch

from noetrail.deadline import ReadTimeout, check_deadline, read_deadline
from noetrail.frontmatter import parse_frontmatter
from noetrail.mcp import NoetrailServer, ToolFailure
from noetrail.store import vault_lock
from tests.test_search_index import VaultHarness


class ReadDeadlineTest(VaultHarness, unittest.TestCase):
    def test_scan_aborts_and_server_remains_usable(self) -> None:
        self.capture("First", "synthetic first")
        self.capture("Second", "synthetic second")
        server = NoetrailServer(self.layout())
        clock = [0.0]

        def slow_read(path):
            result = parse_frontmatter(path)
            clock[0] = 31.0
            return result

        with (
            patch("noetrail.deadline.time.monotonic", side_effect=lambda: clock[0]),
            patch("noetrail.entries.parse_frontmatter", side_effect=slow_read),
        ):
            with self.assertRaises(ToolFailure) as failure:
                server.call_tool("inventory", {})
        self.assertEqual(failure.exception.code, "read_timeout")
        # A writer must be able to take the lock after the interrupted reader.
        with vault_lock(self.data, exclusive=True):
            pass
        self.assertEqual(server.call_tool("inventory", {})["entry_count"], 2)

    def test_nested_deadline_cannot_extend_parent_and_resets_on_error(self) -> None:
        clock = [0.0]
        with patch("noetrail.deadline.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ReadTimeout), read_deadline(1):
                with read_deadline(30):
                    clock[0] = 2.0
                    check_deadline()
            check_deadline()
            with read_deadline(3):
                check_deadline()

    def test_final_checkpoint_rejects_late_result(self) -> None:
        clock = [0.0]
        with patch("noetrail.deadline.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ReadTimeout), read_deadline(1):
                clock[0] = 2.0

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from noetrail.errors import ConfigurationError
from noetrail.layout import resolve_layout
from noetrail.mcp import NoetrailServer, ToolFailure
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

READ_TOOLS = (
    ("inventory", {}),
    ("search", {"query": "lighthouse"}),
    ("review_queue", {"limit": 10}),
    ("list_trash", {}),
)


class InProcessReadTest(unittest.TestCase):
    """The in-process read path must behave exactly like a separate CLI run."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        (self.root / ".knowledge").mkdir()
        (self.root / ".knowledge" / "SPEC.md").write_text("test", encoding="utf-8")
        (self.root / ".knowledge" / "relation-types.yaml").write_text(
            "relations:\n"
            "  related_to:\n"
            "    symmetric: true\n",
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge" / "packs" / "knowledge-core",
            self.root / ".knowledge" / "packs" / "knowledge-core",
        )

        self.kept = self.capture("A lighthouse on the northern cliff", "Lighthouse")
        self.related = self.capture("A harbour worth remembering", "Harbour")
        self.run_cli("relate", self.kept, "related_to", self.related)
        discarded = self.capture("A note that will be discarded", "Discarded")
        self.run_cli("trash", discarded)

        layout = resolve_layout(root=str(self.root))
        self.in_process = NoetrailServer(layout, in_process_reads=True)
        self.subprocess_server = NoetrailServer(layout, in_process_reads=False)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [*CLI_COMMAND, "--root", str(self.root), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def capture(self, text: str, title: str) -> str:
        completed = self.run_cli(
            "capture", "--type", "thought", "--title", title, "--text", text
        )
        return str(json.loads(completed.stdout)["id"])

    def test_both_paths_return_the_same_result_for_every_read_tool(self) -> None:
        cases = [
            *READ_TOOLS,
            ("get_entry", {"id": self.kept}),
            ("relations", {"id": self.kept}),
        ]
        for name, arguments in cases:
            with self.subTest(tool=name):
                self.assertEqual(
                    self.in_process.call_tool(name, dict(arguments)),
                    self.subprocess_server.call_tool(name, dict(arguments)),
                )

    def test_both_paths_report_the_same_failure_for_a_missing_entry(self) -> None:
        arguments = {"id": "kn_" + "0" * 32}
        with self.assertRaises(ToolFailure) as in_process:
            self.in_process.call_tool("get_entry", dict(arguments))
        with self.assertRaises(ToolFailure) as separate:
            self.subprocess_server.call_tool("get_entry", dict(arguments))
        self.assertEqual(str(in_process.exception), str(separate.exception))
        self.assertIn("does not exist", str(in_process.exception))

    def test_in_process_read_writes_nothing_to_the_protocol_stream(self) -> None:
        # The server speaks MCP over stdout, so CLI output must never reach it.
        original = sys.stdout
        observed = io.StringIO()
        sys.stdout = observed
        try:
            result = self.in_process.call_tool("search", {"query": "lighthouse"})
        finally:
            sys.stdout = original
        self.assertEqual(observed.getvalue(), "")
        self.assertIsInstance(result, dict)

    def test_an_unexpected_cli_defect_becomes_a_tool_failure(self) -> None:
        def explode(_arguments: list[str]) -> list[str]:
            raise RuntimeError("synthetic defect")

        original = self.in_process._cli_arguments
        self.in_process._cli_arguments = explode  # type: ignore[method-assign]
        try:
            with self.assertRaises(ToolFailure) as failure:
                self.in_process.call_tool("inventory", {})
        finally:
            self.in_process._cli_arguments = original  # type: ignore[method-assign]
        self.assertIn("synthetic defect", str(failure.exception))

        # The server stays usable after the defect.
        self.assertIsInstance(self.in_process.call_tool("inventory", {}), dict)

    def test_system_exit_preserves_process_exit_semantics(self) -> None:
        for code in (None, 0):
            with self.subTest(code=code):
                with patch("noetrail.mcp.run_command", side_effect=SystemExit(code)):
                    self.assertEqual(
                        self.in_process.call_tool("inventory", {}),
                        {"ok": True},
                    )

        for code, expected in (
            (7, "CLI exited with 7"),
            ("synthetic failure", "synthetic failure"),
        ):
            with self.subTest(code=code):
                with patch("noetrail.mcp.run_command", side_effect=SystemExit(code)):
                    with self.assertRaises(ToolFailure) as failure:
                        self.in_process.call_tool("inventory", {})
                self.assertEqual(str(failure.exception), expected)

    def test_explicit_know_script_handles_reads_in_a_subprocess(self) -> None:
        script = self.root / "custom-cli.py"
        script.write_text(
            "import json\n"
            'print(json.dumps({"implementation": "custom-script"}))\n',
            encoding="utf-8",
        )
        layout = resolve_layout(root=str(self.root))
        server = NoetrailServer(
            layout,
            know_script=script,
            in_process_reads=True,
        )

        self.assertEqual(
            server.call_tool("inventory", {}),
            {"implementation": "custom-script"},
        )

    def test_reads_are_in_process_by_default_and_can_be_forced_to_subprocess(
        self,
    ) -> None:
        layout = resolve_layout(root=str(self.root))
        self.assertTrue(NoetrailServer(layout).in_process_reads)
        completed = subprocess.run(
            [sys.executable, "-m", "noetrail.mcp", "--help"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("--subprocess-reads", completed.stdout)
        self.assertIn(
            "--attachment-delivery-marker-template",
            completed.stdout,
        )
        self.assertIn(
            "--attachment-delivery-marker-root",
            completed.stdout,
        )

    def test_delivery_marker_template_is_bounded_and_requires_an_outbox(
        self,
    ) -> None:
        layout = resolve_layout(root=str(self.root))
        for template in (
            "[image:no-placeholder]",
            "[image:{path}:{path}]",
            "[image:{unknown}]",
            "[image:{path}]\nsecond line",
        ):
            with self.subTest(template=template):
                with self.assertRaises(ConfigurationError):
                    NoetrailServer(
                        layout,
                        attachment_outbox=self.root / "outbox",
                        attachment_delivery_marker_template=template,
                    )

        with self.assertRaises(ConfigurationError):
            NoetrailServer(
                layout,
                attachment_delivery_marker_template="[image:{path}]",
            )

        with self.assertRaises(ConfigurationError):
            NoetrailServer(
                layout,
                attachment_outbox=self.root / "outbox",
                attachment_delivery_marker_root=self.root,
            )

        with self.assertRaises(ConfigurationError):
            NoetrailServer(
                layout,
                attachment_outbox=self.root / "outside",
                attachment_delivery_marker_template="[image:{path}]",
                attachment_delivery_marker_root=self.root / "workspace",
            )


if __name__ == "__main__":
    unittest.main()

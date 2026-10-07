"""The one-command entry point: `noetrail quickstart`."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class QuickstartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.instance = self.base / "instance"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI_COMMAND, "--builtins-root", str(BUILTINS_ROOT), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def quickstart(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_cli("quickstart", "--path", str(self.instance), *extra)

    def test_one_command_produces_a_usable_instance(self) -> None:
        result = self.quickstart("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)

        data = Path(report["data_root"])
        config = Path(report["config_root"])
        self.assertEqual(data, self.instance / "data")
        self.assertEqual(config, self.instance / "config")
        for relative in ("vault", "trash", "imports/raw", "imports/work"):
            self.assertTrue((data / relative).is_dir(), relative)
        self.assertTrue((config / "packs").is_dir())
        self.assertEqual(len(report["created_entries"]), 3)

        validated = self.run_cli(
            "--data-root", str(data), "--config-root", str(config), "validate"
        )
        self.assertEqual(validated.returncode, 0, validated.stdout)
        searched = self.run_cli(
            "--data-root", str(data), "--config-root", str(config), "search", ""
        )
        self.assertEqual(json.loads(searched.stdout)["total"], 3)

    def test_the_printed_mcp_block_is_the_configuration_a_client_takes(
        self,
    ) -> None:
        report = json.loads(self.quickstart("--json").stdout)
        server = report["mcp_config"]["mcpServers"]["noetrail"]
        self.assertEqual(server["command"], "noetrail-mcp")
        self.assertEqual(
            server["args"],
            [
                "--data-root",
                report["data_root"],
                "--config-root",
                report["config_root"],
            ],
        )
        # Absolute roots, because a client resolves them from its own cwd.
        for value in server["args"][1::2]:
            self.assertTrue(Path(value).is_absolute(), value)

    def test_the_printed_walkthrough_names_the_next_commands(self) -> None:
        result = self.quickstart()
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("data root", "review", "inventory", "mcpServers"):
            self.assertIn(expected, result.stdout)

    def test_repeating_it_adds_nothing_and_still_validates(self) -> None:
        self.assertEqual(self.quickstart("--json").returncode, 0)
        repeated = self.quickstart("--json")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        report = json.loads(repeated.stdout)
        self.assertEqual(report["created_entries"], [])
        self.assertEqual(report["created_directories"], [])
        validated = self.run_cli(
            "--data-root",
            report["data_root"],
            "--config-root",
            report["config_root"],
            "validate",
        )
        self.assertEqual(validated.returncode, 0, validated.stdout)

    def test_the_sample_relations_resolve_and_one_stays_unresolved(self) -> None:
        report = json.loads(self.quickstart("--json").stdout)
        roots = [
            "--data-root",
            report["data_root"],
            "--config-root",
            report["config_root"],
        ]
        entry = next(
            item
            for item in report["created_entries"]
            if item["title"].startswith("Relations are typed")
        )
        shown = json.loads(self.run_cli(*roots, "relations", entry["id"]).stdout)
        self.assertEqual(len(shown["outgoing"]), 1)
        self.assertEqual(shown["outgoing"][0]["target_title"], "What Noetrail stores")
        # The deliberately dangling reference is stored, not dropped.
        queued = json.loads(self.run_cli(*roots, "review").stdout)
        self.assertIn("a note you have not written yet", json.dumps(queued))

    def test_the_combined_root_option_is_refused(self) -> None:
        result = self.run_cli("--root", str(self.base), "quickstart")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separated instance", result.stderr)


if __name__ == "__main__":
    unittest.main()

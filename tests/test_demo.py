from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest

from noetrail.migrations import CURRENT_SCHEMA_VERSION
from tests import CLI_COMMAND

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEMO_DATA = REPOSITORY_ROOT / "demo" / "data"
DEMO_CONFIG = REPOSITORY_ROOT / "demo" / "config"
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class SyntheticDemoTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(DEMO_DATA),
                "--config-root",
                str(DEMO_CONFIG),
                "--builtins-root",
                str(BUILTINS_ROOT),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_demo_validates_and_doctor_is_clean(self) -> None:
        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("2 active and 0 trashed", validated.stdout)

        doctor = self.run_cli("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        report = json.loads(doctor.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["entry_schema_versions"],
            {str(CURRENT_SCHEMA_VERSION): 2},
        )

    def test_demo_pack_is_discoverable_and_searchable(self) -> None:
        listed = self.run_cli("schema", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        registry = json.loads(listed.stdout)
        identifiers = {item["id"] for item in registry["types"]}
        self.assertIn("travel/destination", identifiers)

        searched = self.run_cli("search", "harbor")
        self.assertEqual(searched.returncode, 0, searched.stderr)
        page = json.loads(searched.stdout)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["title"], "Azure Harbor")

    def test_demo_contains_only_explicitly_synthetic_content(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY_ROOT / "demo").rglob("*"))
            if path.is_file()
        )
        self.assertIn("synthetic", combined.casefold())
        self.assertNotIn('sensitivity: "personal"', combined)
        self.assertNotIn('sensitivity: "sensitive"', combined)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from noetrail.migrations import CURRENT_SCHEMA_VERSION
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class InitializationAndDoctorTest(unittest.TestCase):
    def run_cli(
        self,
        data: Path,
        config: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(data),
                "--config-root",
                str(config),
                "--builtins-root",
                str(BUILTINS_ROOT),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_is_idempotent_and_doctor_reports_only_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            data = base / "private-data"
            config = base / "instance-config"

            initialized = self.run_cli(data, config, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            result = json.loads(initialized.stdout)
            self.assertTrue(result["initialized"])
            self.assertFalse(result["idempotent"])
            for relative in (
                "vault",
                "vault/.locks",
                "trash",
                "imports/raw",
                "imports/work",
            ):
                self.assertTrue((data / relative).is_dir())
            self.assertTrue((config / "packs").is_dir())
            self.assertFalse((data / ".knowledge").exists())

            repeated = self.run_cli(data, config, "init")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertTrue(json.loads(repeated.stdout)["idempotent"])

            captured = self.run_cli(
                data,
                config,
                "capture",
                "--type",
                "note",
                "--title",
                "Private diagnostic title",
                "--text",
                "PRIVATE_BODY_MARKER",
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)
            captured_result = json.loads(captured.stdout)

            doctor = self.run_cli(data, config, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            diagnosis = json.loads(doctor.stdout)
            self.assertTrue(diagnosis["ok"])
            self.assertEqual(
                diagnosis["entry_schema_versions"],
                {str(CURRENT_SCHEMA_VERSION): 1},
            )
            self.assertNotIn("Private diagnostic title", doctor.stdout)
            self.assertNotIn("PRIVATE_BODY_MARKER", doctor.stdout)

            entry = data / captured_result["created"]
            content = entry.read_text(encoding="utf-8").replace(
                f"schema_version: {CURRENT_SCHEMA_VERSION}",
                "schema_version: 99",
            )
            entry.write_text(content, encoding="utf-8")
            incompatible = self.run_cli(data, config, "doctor")
            self.assertEqual(incompatible.returncode, 1)
            incompatible_result = json.loads(incompatible.stdout)
            self.assertFalse(incompatible_result["ok"])
            self.assertEqual(
                incompatible_result["entry_schema_versions"],
                {"99": 1},
            )
            self.assertNotIn("Private diagnostic title", incompatible.stdout)

    def test_doctor_reports_invalid_pack_without_reading_vault_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            data = base / "data"
            config = base / "config"
            initialized = self.run_cli(data, config, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            bad_pack = config / "packs" / "bad"
            bad_pack.mkdir()
            (bad_pack / "pack.yaml").write_text(
                'format_version: 1\nid: "bad"\nunknown: true\n',
                encoding="utf-8",
            )
            private_entry = data / "vault" / "private.md"
            private_entry.write_text(
                "PRIVATE_VAULT_CONTENT_THAT_MUST_NOT_BE_READ",
                encoding="utf-8",
            )

            doctor = self.run_cli(data, config, "doctor")
            self.assertEqual(doctor.returncode, 1)
            diagnosis = json.loads(doctor.stdout)
            self.assertFalse(diagnosis["ok"])
            self.assertEqual(diagnosis["checks"][0]["name"], "schema_registry")
            self.assertNotIn("PRIVATE_VAULT_CONTENT", doctor.stdout)

    def test_doctor_reports_missing_root_as_layout_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            doctor = self.run_cli(
                base / "missing-data",
                base / "missing-config",
                "doctor",
            )
            self.assertEqual(doctor.returncode, 1)
            diagnosis = json.loads(doctor.stdout)
            self.assertFalse(diagnosis["ok"])
            self.assertEqual(diagnosis["checks"][0]["name"], "layout")


if __name__ == "__main__":
    unittest.main()

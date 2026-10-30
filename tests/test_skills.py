from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from noetrail.cli import run_command
from noetrail.commands import skills as skills_module
from noetrail.errors import ConfigurationError, Conflict, InvalidRequest, StorageError
from noetrail.layout import LayoutError, default_skills_root
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_SKILLS = sorted(
    path.parent.name for path in (REPOSITORY_ROOT / "skills").glob("*/SKILL.md")
)


def write_skill(root: Path, name: str, body: str = "synthetic skill") -> Path:
    skill = root / name
    (skill / "agents").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    (skill / "agents" / "openai.yaml").write_text(
        f"interface:\n  display_name: {name}\n", encoding="utf-8"
    )
    return skill


class SkillsRootResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.application = self.base / "application"
        self.application.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_checkout_resolves_to_its_own_skills_directory(self) -> None:
        self.assertEqual(default_skills_root(), REPOSITORY_ROOT / "skills")
        self.assertEqual(
            default_skills_root(REPOSITORY_ROOT), REPOSITORY_ROOT / "skills"
        )

    def test_an_installed_layout_resolves_below_share_noetrail(self) -> None:
        installed = self.application / "share" / "noetrail" / "skills"
        write_skill(installed, "synthetic-skill")
        self.assertEqual(default_skills_root(self.application), installed)

    def test_a_checkout_directory_wins_over_an_installed_one(self) -> None:
        write_skill(self.application / "share" / "noetrail" / "skills", "one")
        write_skill(self.application / "skills", "two")
        self.assertEqual(
            default_skills_root(self.application), self.application / "skills"
        )

    def test_a_directory_without_any_skill_is_not_accepted(self) -> None:
        (self.application / "skills" / "empty").mkdir(parents=True)
        (self.application / "share" / "noetrail" / "skills").mkdir(parents=True)
        with self.assertRaisesRegex(LayoutError, "No portable skills"):
            default_skills_root(self.application)

    def test_there_is_no_legacy_location(self) -> None:
        write_skill(
            self.application / "share" / "personal-knowledge-vault" / "skills",
            "legacy",
        )
        with self.assertRaises(LayoutError):
            default_skills_root(self.application)


class SkillsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.application = self.base / "application"
        self.source = self.application / "share" / "noetrail" / "skills"
        write_skill(self.source, "alpha")
        write_skill(self.source, "beta")
        self.target = self.base / "host" / "skills"
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.base / "xdg"),
                "NOETRAIL_DATA_ROOT": "",
                "NOETRAIL_CONFIG_ROOT": "",
                "KNOWLEDGE_DATA_ROOT": "",
                "KNOWLEDGE_CONFIG_ROOT": "",
            },
        )
        self.environment.start()
        self.installed = patch(
            "noetrail.layout.default_application_root",
            return_value=self.application,
        )
        self.installed.start()

    def tearDown(self) -> None:
        self.installed.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run_command(list(arguments))
        self.assertEqual(status, 0)
        return output.getvalue()

    def test_path_prints_the_installed_directory(self) -> None:
        self.assertEqual(self.run_cli("skills", "path").strip(), str(self.source))

    def test_path_json_names_every_skill(self) -> None:
        report = json.loads(self.run_cli("skills", "path", "--json"))
        self.assertEqual(
            report, {"skills_root": str(self.source), "skills": ["alpha", "beta"]}
        )

    def test_path_ignores_entries_that_are_not_skills(self) -> None:
        (self.source / "Not_A_Name").mkdir()
        (self.source / "Not_A_Name" / "SKILL.md").write_text("x", encoding="utf-8")
        (self.source / "no-skill-file").mkdir()
        (self.source / "loose.md").write_text("x", encoding="utf-8")
        (self.source / "linked").symlink_to(self.source / "alpha")
        report = json.loads(self.run_cli("skills", "path", "--json"))
        self.assertEqual(report["skills"], ["alpha", "beta"])

    def test_a_missing_skills_directory_is_a_configuration_error(self) -> None:
        with patch(
            "noetrail.layout.default_application_root",
            return_value=self.base / "nowhere",
        ):
            with self.assertRaisesRegex(ConfigurationError, "No portable skills"):
                run_command(["skills", "path"])

    def test_export_copies_every_skill_into_a_fresh_directory(self) -> None:
        output = self.run_cli("skills", "export", str(self.target))
        self.assertIn(f"Exported 2 skills to {self.target}", output)
        for name in ("alpha", "beta"):
            for relative in ("SKILL.md", "agents/openai.yaml"):
                copied = self.target / name / relative
                self.assertEqual(
                    copied.read_bytes(),
                    (self.source / name / relative).read_bytes(),
                )
                self.assertIn(str(copied), output)
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            sorted(path.name for path in self.target.iterdir()), ["alpha", "beta"]
        )

    def test_export_json_lists_the_written_paths(self) -> None:
        report = json.loads(
            self.run_cli("skills", "export", str(self.target), "--json")
        )
        self.assertEqual(report["skills_root"], str(self.source))
        self.assertEqual(report["target"], str(self.target))
        self.assertEqual(report["skills"], ["alpha", "beta"])
        self.assertEqual(
            report["written"],
            [
                str(self.target / name / relative)
                for name in ("alpha", "beta")
                for relative in ("SKILL.md", "agents/openai.yaml")
            ],
        )

    def test_export_skips_hidden_files(self) -> None:
        (self.source / "alpha" / ".DS_Store").write_text("x", encoding="utf-8")
        (self.source / "alpha" / ".cache").mkdir()
        (self.source / "alpha" / ".cache" / "x").write_text("x", encoding="utf-8")
        self.run_cli("skills", "export", str(self.target))
        self.assertFalse((self.target / "alpha" / ".DS_Store").exists())
        self.assertFalse((self.target / "alpha" / ".cache").exists())

    def test_export_accepts_an_existing_empty_skill_directory(self) -> None:
        (self.target / "alpha").mkdir(parents=True)
        self.run_cli("skills", "export", str(self.target))
        self.assertTrue((self.target / "alpha" / "SKILL.md").is_file())

    def test_export_refuses_an_existing_skill_without_force(self) -> None:
        mine = self.target / "beta" / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("my own edit", encoding="utf-8")
        with self.assertRaisesRegex(Conflict, "beta; pass --force"):
            run_command(["skills", "export", str(self.target)])
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own edit")
        # Nothing is written when any skill is refused.
        self.assertFalse((self.target / "alpha").exists())

    def test_force_overwrites_shipped_files_and_keeps_added_ones(self) -> None:
        self.run_cli("skills", "export", str(self.target))
        shipped = self.target / "alpha" / "SKILL.md"
        shipped.write_text("stale", encoding="utf-8")
        added = self.target / "alpha" / "notes.md"
        added.write_text("kept", encoding="utf-8")
        self.run_cli("skills", "export", str(self.target), "--force")
        self.assertEqual(
            shipped.read_bytes(), (self.source / "alpha" / "SKILL.md").read_bytes()
        )
        self.assertEqual(added.read_text(encoding="utf-8"), "kept")
        shipped.chmod(0o640)
        self.run_cli("skills", "export", str(self.target), "--force")
        self.assertEqual(shipped.stat().st_mode & 0o777, 0o640)

    def test_export_never_follows_a_link_in_the_target(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.target.mkdir(parents=True)
        (self.target / "alpha").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Conflict, "symbolic link"):
            run_command(["skills", "export", str(self.target), "--force"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_export_never_writes_through_a_linked_file_or_subdirectory(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "victim").write_text("untouched", encoding="utf-8")
        (self.target / "alpha").mkdir(parents=True)
        (self.target / "alpha" / "SKILL.md").symlink_to(outside / "victim")
        with self.assertRaisesRegex(Conflict, "symbolic link"):
            run_command(["skills", "export", str(self.target), "--force"])
        (self.target / "alpha" / "SKILL.md").unlink()
        (self.target / "alpha" / "agents").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(Conflict, "symbolic link"):
            run_command(["skills", "export", str(self.target), "--force"])
        self.assertEqual(
            (outside / "victim").read_text(encoding="utf-8"), "untouched"
        )
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["victim"])

    def test_export_refuses_a_file_where_a_directory_belongs(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "alpha").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(Conflict, "Expected a directory"):
            run_command(["skills", "export", str(self.target), "--force"])

    def test_export_refuses_a_directory_where_a_file_belongs(self) -> None:
        (self.target / "alpha" / "SKILL.md").mkdir(parents=True)
        with self.assertRaisesRegex(Conflict, "Expected a file"):
            run_command(["skills", "export", str(self.target), "--force"])

    def test_export_refuses_a_target_that_is_a_file(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(InvalidRequest, "not a directory"):
            run_command(["skills", "export", str(self.target)])

    def test_export_refuses_a_target_inside_the_source(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "skills source"):
            run_command(["skills", "export", str(self.source / "alpha")])

    def test_export_refuses_a_target_inside_a_data_or_config_root(self) -> None:
        data = self.base / "data"
        config = self.base / "config"
        for option, root in (
            ("--data-root", data),
            ("--config-root", config),
            ("--root", data),
        ):
            with self.assertRaisesRegex(InvalidRequest, "data or configuration"):
                run_command(
                    [option, str(root), "skills", "export", str(root / "skills")]
                )
        with patch.dict(os.environ, {"NOETRAIL_DATA_ROOT": str(data)}):
            with self.assertRaisesRegex(InvalidRequest, "data or configuration"):
                run_command(["skills", "export", str(data / "skills")])
        default_config = self.base / "xdg" / "noetrail"
        with self.assertRaisesRegex(InvalidRequest, "data or configuration"):
            run_command(["skills", "export", str(default_config / "skills")])
        self.assertFalse(data.exists())
        self.assertFalse(config.exists())

    def test_export_refuses_a_linked_entry_in_the_source(self) -> None:
        outside = self.base / "secret.txt"
        outside.write_text("not a skill file", encoding="utf-8")
        (self.source / "alpha" / "agents" / "linked.yaml").symlink_to(outside)
        with self.assertRaisesRegex(ConfigurationError, "non-regular file"):
            run_command(["skills", "export", str(self.target)])
        self.assertFalse(self.target.exists())

    def test_export_refuses_a_linked_directory_in_the_source(self) -> None:
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "file.md").write_text("x", encoding="utf-8")
        (self.source / "beta" / "references").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(ConfigurationError, "symbolic link"):
            run_command(["skills", "export", str(self.target)])
        self.assertFalse(self.target.exists())

    def test_a_write_failure_is_a_storage_error(self) -> None:
        with patch.object(
            skills_module, "_write_file", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(StorageError, "disk full"):
                run_command(["skills", "export", str(self.target)])

    def test_a_failed_replace_leaves_no_temporary_file(self) -> None:
        with patch.object(
            skills_module.os, "replace", side_effect=OSError("read-only")
        ):
            with self.assertRaises(StorageError):
                run_command(["skills", "export", str(self.target)])
        self.assertEqual(
            [path for path in self.target.rglob("*") if path.is_file()], []
        )


class SkillsCommandProcessTest(unittest.TestCase):
    """The shipped skills, through the console entry point, with no vault."""

    def test_path_and_export_need_no_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = temporary_root(raw)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("NOETRAIL_", "KNOWLEDGE_"))
            }
            environment["XDG_CONFIG_HOME"] = str(base / "xdg")
            located = subprocess.run(
                [*CLI_COMMAND, "skills", "path", "--json"],
                cwd=base,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(located.returncode, 0, located.stderr)
            report = json.loads(located.stdout)
            self.assertEqual(report["skills_root"], str(REPOSITORY_ROOT / "skills"))
            self.assertEqual(report["skills"], SHIPPED_SKILLS)
            self.assertEqual(len(SHIPPED_SKILLS), 6)

            target = base / "exported"
            exported = subprocess.run(
                [*CLI_COMMAND, "skills", "export", str(target), "--json"],
                cwd=base,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(
                sorted(path.name for path in target.iterdir()), SHIPPED_SKILLS
            )
            refused = subprocess.run(
                [*CLI_COMMAND, "skills", "export", str(target)],
                cwd=base,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("pass --force", refused.stderr)
            self.assertEqual(sorted(path.name for path in base.iterdir()), [
                "exported"
            ])


if __name__ == "__main__":
    unittest.main()

"""`noetrail connect --write`: merge the server entry into a JSON host's file."""

from __future__ import annotations

import argparse
import contextlib
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from noetrail.cli import build_parser, command_is_mutating, run_command
from noetrail.commands import connect as connect_module
from noetrail.commands import connect_write
from noetrail.errors import Conflict, InvalidRequest, StorageError
from noetrail.layout import LayoutError, resolve_layout
from tests import CLI_COMMAND, temporary_root
from tests.test_connect import REPORT_KEYS, without_root_variables

WRITE_KEYS = {"path", "action", "backup", "applied", "reason"}
BACKUP_NAME = re.compile(r"^mcp\.json\.\d{8}T\d{6}Z\.bak$")
FIXED_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
FIXED_STAMP = "20261030T120000Z"
OTHER_SERVER = {"command": "example-server", "args": ["--flag"]}


class ConnectWriteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.home = self.base / "home"
        self.home.mkdir()
        self.data = self.home / "noetrail" / "data"
        self.config = self.home / "noetrail" / "config"
        self.data.mkdir(parents=True)
        self.config.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        environment = without_root_variables()
        environment["HOME"] = str(self.home)
        environment.pop("APPDATA", None)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.directory = contextlib.chdir(self.project)
        self.directory.__enter__()
        self.cursor_file = self.home / ".cursor" / "mcp.json"

    def tearDown(self) -> None:
        self.directory.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run_command(["connect", *arguments])
        self.assertEqual(status, 0)
        return output.getvalue()

    def refused(
        self, error: type[Exception], pattern: str, *arguments: str
    ) -> str:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(error, pattern),
        ):
            run_command(["connect", *arguments])
        return output.getvalue()

    def write_json(self, client: str, *arguments: str) -> dict[str, object]:
        report = json.loads(
            self.invoke("--client", client, "--write", "--json", *arguments)
        )
        assert isinstance(report, dict)
        return report

    def entry(self, client: str, *arguments: str) -> dict[str, object]:
        report = json.loads(self.invoke("--client", client, "--json", *arguments))
        server = report["server"]
        assert isinstance(server, dict)
        return server

    def files_in(self, directory: Path) -> list[str]:
        return sorted(path.name for path in directory.iterdir())

    def make_cursor_file(self, content: str) -> None:
        self.cursor_file.parent.mkdir(exist_ok=True)
        self.cursor_file.write_text(content, encoding="utf-8")


class PreviewAndApplyTest(ConnectWriteTestCase):
    def test_the_preview_writes_nothing(self) -> None:
        (self.home / ".cursor").mkdir()
        text = self.invoke("--client", "cursor", "--write")
        self.assertIn("preview, nothing written yet", text)
        self.assertIn(f"  file      {self.cursor_file}", text)
        self.assertIn("  action    create a new file with mcpServers.noetrail", text)
        self.assertIn("  backup    none (new file)", text)
        self.assertTrue(
            text.rstrip().endswith(
                "Run the same command with --apply to write it."
            )
        )
        self.assertEqual(self.files_in(self.home / ".cursor"), [])

        self.make_cursor_file('{"mcpServers": {}}\n')
        report = self.write_json("cursor")
        self.assertEqual(report["write"]["action"], "update")
        self.assertFalse(report["write"]["applied"])
        self.assertEqual(self.files_in(self.home / ".cursor"), ["mcp.json"])
        self.assertEqual(
            self.cursor_file.read_text(encoding="utf-8"), '{"mcpServers": {}}\n'
        )

    def test_apply_creates_an_owner_only_file(self) -> None:
        (self.home / ".cursor").mkdir()
        report = self.write_json("cursor", "--apply")
        self.assertEqual(
            report["write"],
            {
                "path": str(self.cursor_file),
                "action": "create",
                "backup": None,
                "applied": True,
                "reason": None,
            },
        )
        text = self.cursor_file.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n  "mcpServers": {', text)
        self.assertEqual(
            json.loads(text), {"mcpServers": {"noetrail": self.entry("cursor")}}
        )
        self.assertEqual(stat.S_IMODE(self.cursor_file.stat().st_mode), 0o600)
        self.assertEqual(self.files_in(self.home / ".cursor"), ["mcp.json"])

    def test_the_text_after_apply_names_the_skills_line(self) -> None:
        (self.home / ".cursor").mkdir()
        text = self.invoke("--client", "cursor", "--write", "--apply")
        self.assertIn("Cursor (cursor): written", text)
        self.assertIn("noetrail skills export ~/.agents/skills", text)
        self.assertNotIn("--apply to write it", text)

    def test_an_update_keeps_other_servers_and_keys(self) -> None:
        original = {
            "$schema": "https://example.invalid/schema.json",
            "mcpServers": {"other": OTHER_SERVER},
            "theme": "dark",
            "nested": {"list": [1, 2.5, None, True], "text": "ümlaut"},
        }
        self.make_cursor_file(json.dumps(original, indent=2) + "\n")
        os.chmod(self.cursor_file, 0o640)
        with patch.object(connect_write, "utc_now", return_value=FIXED_NOW):
            report = self.write_json("cursor", "--apply")
        backup = self.cursor_file.with_name(f"mcp.json.{FIXED_STAMP}.bak")
        self.assertEqual(report["write"]["action"], "update")
        self.assertEqual(report["write"]["backup"], str(backup))
        written = json.loads(self.cursor_file.read_text(encoding="utf-8"))
        self.assertEqual(
            list(written), ["$schema", "mcpServers", "theme", "nested"]
        )
        self.assertEqual(written["nested"], original["nested"])
        self.assertEqual(written["theme"], "dark")
        self.assertEqual(
            written["mcpServers"],
            {"other": OTHER_SERVER, "noetrail": self.entry("cursor")},
        )
        # The mode is kept; the backup is owner-only and byte-identical.
        self.assertEqual(stat.S_IMODE(self.cursor_file.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(backup.read_text(encoding="utf-8")), original
        )
        self.assertRegex(backup.name, BACKUP_NAME)

    def test_an_unchanged_entry_writes_nothing_and_backs_nothing_up(self) -> None:
        (self.home / ".cursor").mkdir()
        self.invoke("--client", "cursor", "--write", "--apply")
        before = self.cursor_file.read_bytes()
        report = self.write_json("cursor", "--apply")
        self.assertEqual(report["write"]["action"], "unchanged")
        self.assertIsNone(report["write"]["backup"])
        self.assertFalse(report["write"]["applied"])
        self.assertIn("already holds this entry", report["write"]["reason"])
        self.assertEqual(self.cursor_file.read_bytes(), before)
        self.assertEqual(self.files_in(self.home / ".cursor"), ["mcp.json"])
        text = self.invoke("--client", "cursor", "--write")
        self.assertIn("nothing to write", text)
        self.assertNotIn("--apply", text)

    def test_an_empty_file_is_an_empty_object(self) -> None:
        self.make_cursor_file("\n")
        report = self.write_json("cursor", "--apply")
        self.assertEqual(report["write"]["action"], "update")
        self.assertIsNotNone(report["write"]["backup"])
        self.assertIn(
            "noetrail",
            json.loads(self.cursor_file.read_text(encoding="utf-8"))["mcpServers"],
        )

    def test_json_keeps_every_existing_key(self) -> None:
        (self.home / ".cursor").mkdir()
        report = self.write_json("cursor")
        self.assertEqual(set(report), REPORT_KEYS | {"write"})
        self.assertEqual(set(report["write"]), WRITE_KEYS)
        plain = json.loads(self.invoke("--client", "cursor", "--json"))
        self.assertEqual(set(plain), REPORT_KEYS)


class ConflictTest(ConnectWriteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original = {"mcpServers": {"noetrail": OTHER_SERVER}}
        self.make_cursor_file(json.dumps(self.original) + "\n")

    def test_a_different_entry_is_refused_and_both_are_shown(self) -> None:
        for extra in ((), ("--apply",)):
            with self.subTest(extra=extra):
                text = self.refused(
                    Conflict, "pass --force", "--client", "cursor", "--write", *extra
                )
                self.assertIn("Cursor (cursor): refused", text)
                self.assertIn("  action    refuse", text)
                self.assertIn("Existing mcpServers.noetrail:", text)
                self.assertIn('"command": "example-server"', text)
                self.assertIn("New mcpServers.noetrail:", text)
                self.assertIn('"command": "noetrail-mcp"', text)
                self.assertEqual(
                    json.loads(self.cursor_file.read_text(encoding="utf-8")),
                    self.original,
                )
                self.assertEqual(self.files_in(self.home / ".cursor"), ["mcp.json"])

    def test_the_json_preview_reports_the_refusal(self) -> None:
        output = self.refused(
            Conflict, "pass --force", "--client", "cursor", "--write", "--json"
        )
        write = json.loads(output)["write"]
        self.assertEqual(write["action"], "refuse")
        self.assertIsNone(write["backup"])
        self.assertFalse(write["applied"])
        self.assertIn("pass --force", write["reason"])

    def test_force_replaces_it_after_a_backup(self) -> None:
        text = self.invoke("--client", "cursor", "--write", "--force")
        self.assertIn("replaces the existing mcpServers.noetrail (--force)", text)
        self.assertIn("Existing mcpServers.noetrail:", text)
        report = self.write_json("cursor", "--apply", "--force")
        self.assertEqual(report["write"]["action"], "update")
        self.assertTrue(report["write"]["applied"])
        backup = Path(report["write"]["backup"])
        self.assertEqual(
            json.loads(backup.read_text(encoding="utf-8")), self.original
        )
        self.assertEqual(
            json.loads(self.cursor_file.read_text(encoding="utf-8")),
            {"mcpServers": {"noetrail": self.entry("cursor")}},
        )

    def test_true_and_one_are_not_the_same_entry(self) -> None:
        entry = self.entry("opencode")
        entry["enabled"] = 1
        target = self.base / "opencode.json"
        target.write_text(json.dumps({"mcp": {"noetrail": entry}}), encoding="utf-8")
        self.refused(
            Conflict,
            "pass --force",
            "--client",
            "opencode",
            "--write",
            "--config-file",
            str(target),
        )


class BackupTest(ConnectWriteTestCase):
    def test_an_existing_backup_is_never_overwritten(self) -> None:
        self.make_cursor_file('{"mcpServers": {}}\n')
        taken = self.cursor_file.with_name(f"mcp.json.{FIXED_STAMP}.bak")
        taken.write_text("an older backup\n", encoding="utf-8")
        with patch.object(connect_write, "utc_now", return_value=FIXED_NOW):
            preview = self.write_json("cursor")
            report = self.write_json("cursor", "--apply")
        expected = self.cursor_file.with_name(f"mcp.json.{FIXED_STAMP}-1.bak")
        self.assertEqual(preview["write"]["backup"], str(expected))
        self.assertEqual(report["write"]["backup"], str(expected))
        self.assertEqual(taken.read_text(encoding="utf-8"), "an older backup\n")
        self.assertEqual(expected.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')

    def test_a_backup_taken_after_the_preview_moves_to_the_next_name(self) -> None:
        self.make_cursor_file('{"mcpServers": {}}\n')
        host = connect_module.find_host("cursor")
        layout = resolve_layout(data_root=self.data, config_root=self.config)
        report = connect_module.connection(
            host, "console", layout, explicit_builtins=False
        )
        args = build_parser().parse_args(["connect", "--client", "cursor", "--write"])
        with patch.object(connect_write, "utc_now", return_value=FIXED_NOW):
            plan = connect_write.plan_write(
                host, report, layout, args, platform="linux", cwd=self.project
            )
        assert plan.backup is not None
        plan.backup.write_text("raced\n", encoding="utf-8")
        connect_write.apply(plan)
        self.assertEqual(
            plan.backup.name, f"mcp.json.{FIXED_STAMP}-1.bak"
        )
        self.assertTrue(plan.applied)


class FormattingTest(ConnectWriteTestCase):
    def test_four_spaces_and_tabs_are_kept(self) -> None:
        for indent, marker in ((4, '\n    "mcpServers"'), ("\t", '\n\t"mcpServers"')):
            with self.subTest(indent=indent):
                self.make_cursor_file(
                    json.dumps({"mcpServers": {"other": OTHER_SERVER}}, indent=indent)
                )
                self.invoke("--client", "cursor", "--write", "--apply")
                text = self.cursor_file.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("{" + marker))
                self.assertTrue(text.endswith("}\n"))

    def test_detect_indent(self) -> None:
        self.assertEqual(connect_write.detect_indent("{}"), 2)
        self.assertEqual(connect_write.detect_indent('{\n   "a": 1\n}'), 2)
        self.assertEqual(connect_write.detect_indent('{\n    "a": 1\n}'), 4)
        self.assertEqual(connect_write.detect_indent('{\n\t"a": 1\n}'), "\t")


class FileContentRefusalTest(ConnectWriteTestCase):
    def assert_refused(self, content: str | bytes, pattern: str) -> None:
        self.cursor_file.parent.mkdir(exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.cursor_file.write_bytes(content)
        for extra in ((), ("--apply",)):
            with self.subTest(content=content, extra=extra):
                text = self.refused(
                    InvalidRequest, pattern, "--client", "cursor", "--write", *extra
                )
                self.assertIn("  action    refuse", text)
                self.assertEqual(self.cursor_file.read_bytes(), content)
                self.assertEqual(self.files_in(self.home / ".cursor"), ["mcp.json"])

    def test_jsonc_is_refused(self) -> None:
        self.assert_refused(
            '{\n  // my servers\n  "mcpServers": {}\n}\n', "paste the printed block"
        )
        self.assert_refused('{"mcpServers": {},}\n', "JSONC")

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        self.assert_refused("[]\n", "does not hold a JSON object")

    def test_a_server_key_that_is_not_an_object_is_refused(self) -> None:
        self.assert_refused('{"mcpServers": ["noetrail"]}\n', "is not a JSON object")

    def test_repeated_keys_are_refused(self) -> None:
        self.assert_refused('{"theme": 1, "theme": 2}\n', "repeats the key 'theme'")

    def test_text_that_is_not_utf8_is_refused(self) -> None:
        self.assert_refused(b'{"theme": "\xff"}\n', "is not UTF-8")

    def test_an_unreadable_file_is_a_storage_error(self) -> None:
        self.make_cursor_file("{}\n")
        with patch.object(Path, "read_bytes", side_effect=OSError("denied")):
            self.refused(
                StorageError, "Could not read", "--client", "cursor", "--write"
            )


class PathRefusalTest(ConnectWriteTestCase):
    def test_a_linked_file_is_refused_with_its_real_path(self) -> None:
        dotfiles = self.base / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "cursor-mcp.json"
        real.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        (self.home / ".cursor").mkdir()
        self.cursor_file.symlink_to(real)
        text = self.refused(
            InvalidRequest,
            re.escape(f"--config-file {real}"),
            "--client",
            "cursor",
            "--write",
            "--apply",
        )
        self.assertIn("refused", text)
        self.assertEqual(real.read_text(encoding="utf-8"), '{"mcpServers": {}}\n')
        # The real path is the escape hatch.
        report = self.write_json("cursor", "--apply", "--config-file", str(real))
        self.assertTrue(report["write"]["applied"])
        self.assertTrue(self.cursor_file.is_symlink())

    def test_a_linked_directory_or_dangling_link_is_refused(self) -> None:
        dotfiles = self.base / "dotfiles"
        dotfiles.mkdir()
        (self.home / ".cursor").symlink_to(dotfiles)
        self.refused(
            InvalidRequest,
            "dotfile repository",
            "--client",
            "cursor",
            "--write",
        )
        (self.home / ".cursor").unlink()
        (self.home / ".cursor").mkdir()
        self.cursor_file.symlink_to(self.base / "missing.json")
        self.refused(
            InvalidRequest, "symbolic link", "--client", "cursor", "--write", "--apply"
        )
        self.assertFalse((self.base / "missing.json").exists())

    def test_a_link_in_an_explicit_path_is_refused(self) -> None:
        real = self.base / "real"
        real.mkdir()
        (self.base / "linked").symlink_to(real)
        self.refused(
            InvalidRequest,
            "symbolic link",
            "--client",
            "cursor",
            "--write",
            "--config-file",
            str(self.base / "linked" / "mcp.json"),
        )

    def test_a_missing_host_directory_is_refused(self) -> None:
        text = self.refused(
            InvalidRequest,
            "does not look installed here.*--config-file",
            "--client",
            "cursor",
            "--write",
            "--apply",
        )
        self.assertIn("refused", text)
        self.assertFalse((self.home / ".cursor").exists())

    def test_config_file_creates_its_directories(self) -> None:
        target = self.base / "elsewhere" / "deeper" / "mcp.json"
        report = self.write_json(
            "cursor", "--apply", "--config-file", str(target)
        )
        self.assertEqual(report["write"]["path"], str(target))
        self.assertEqual(report["write"]["action"], "create")
        self.assertIn("noetrail", json.loads(target.read_text())["mcpServers"])

    def test_config_file_may_be_relative_or_under_home(self) -> None:
        report = self.write_json("gemini", "--config-file", "settings.json")
        self.assertEqual(report["write"]["path"], str(self.project / "settings.json"))
        report = self.write_json("gemini", "--config-file", "~/custom/settings.json")
        self.assertEqual(
            report["write"]["path"], str(self.home / "custom" / "settings.json")
        )

    def test_a_directory_or_a_file_parent_is_refused(self) -> None:
        self.refused(
            InvalidRequest,
            "Not a regular file",
            "--client",
            "cursor",
            "--write",
            "--config-file",
            str(self.base),
        )
        blocker = self.base / "blocker"
        blocker.write_text("", encoding="utf-8")
        self.refused(
            InvalidRequest,
            "Not a directory",
            "--client",
            "cursor",
            "--write",
            "--config-file",
            str(blocker / "mcp.json"),
        )

    def test_a_noetrail_root_is_refused(self) -> None:
        for root in (self.data, self.config):
            with self.subTest(root=root):
                self.refused(
                    InvalidRequest,
                    "inside a Noetrail data, configuration",
                    "--client",
                    "cursor",
                    "--write",
                    "--apply",
                    "--config-file",
                    str(root / "mcp.json"),
                )
                self.assertFalse((root / "mcp.json").exists())

    def test_the_skills_source_and_builtins_are_protected(self) -> None:
        layout = resolve_layout(data_root=self.data, config_root=self.config)
        args = argparse.Namespace()
        roots = connect_write.protected(args, layout)
        self.assertIn(layout.builtins_root.resolve(), roots)
        with patch.object(
            connect_write, "default_skills_root", return_value=self.base / "skills"
        ):
            self.assertIn(
                self.base / "skills", connect_write.protected(args, layout)
            )
        with patch.object(
            connect_write, "default_skills_root", side_effect=LayoutError("none")
        ):
            self.assertIn(self.data, connect_write.protected(args, layout))

    def test_toml_hosts_only_print(self) -> None:
        for client in ("codex", "zeroclaw"):
            with self.subTest(client=client):
                output = self.refused(
                    InvalidRequest,
                    "TOML.*cannot write it",
                    "--client",
                    client,
                    "--write",
                    "--apply",
                )
                self.assertEqual(output, "")
        self.assertEqual(self.files_in(self.home), ["noetrail"])


class PlatformTest(ConnectWriteTestCase):
    def desktop(self, system: str, *arguments: str) -> dict[str, object]:
        with patch.object(connect_module, "platform_name", return_value=system):
            return self.write_json("claude-desktop", *arguments)

    def test_claude_desktop_on_linux_is_refused_without_config_file(self) -> None:
        with patch.object(connect_module, "platform_name", return_value="linux"):
            self.refused(
                InvalidRequest,
                "no documented configuration file on this platform",
                "--client",
                "claude-desktop",
                "--write",
            )
        target = self.base / "claude_desktop_config.json"
        report = self.desktop("linux", "--apply", "--config-file", str(target))
        self.assertTrue(report["write"]["applied"])

    def test_claude_desktop_on_macos(self) -> None:
        directory = self.home / "Library" / "Application Support" / "Claude"
        directory.mkdir(parents=True)
        with patch.object(connect_module, "platform_name", return_value="macos"):
            text = self.invoke("--client", "claude-desktop", "--write", "--apply")
        report = self.desktop("macos")
        self.assertEqual(report["write"]["action"], "unchanged")
        self.assertEqual(
            report["write"]["path"], str(directory / "claude_desktop_config.json")
        )
        self.assertIn("Restart Claude Desktop", text)

    def test_claude_desktop_on_windows_reads_appdata(self) -> None:
        appdata = self.base / "AppData" / "Roaming"
        (appdata / "Claude").mkdir(parents=True)
        with (
            patch.object(connect_module, "platform_name", return_value="windows"),
            self.assertRaisesRegex(InvalidRequest, "APPDATA is not set"),
        ):
            run_command(["connect", "--client", "claude-desktop", "--write"])
        with patch.dict(os.environ, {"APPDATA": str(appdata)}):
            report = self.desktop("windows")
        self.assertEqual(
            report["write"]["path"],
            str(appdata / "Claude" / "claude_desktop_config.json"),
        )

    def test_dollar_references_expand(self) -> None:
        host = connect_module.find_host("opencode")
        for raw in (
            "$XDG_CONFIG_HOME/opencode/x.json",
            "${XDG_CONFIG_HOME}/opencode/x.json",
        ):
            with self.subTest(raw=raw):
                target = connect_write.default_target(
                    host,
                    raw,
                    platform="linux",
                    environ={"XDG_CONFIG_HOME": str(self.base / "xdg")},
                    cwd=self.project,
                )
                self.assertEqual(target.path, self.base / "xdg" / "opencode" / "x.json")
                self.assertEqual(target.anchor, self.base / "xdg")
                self.assertFalse(target.create_parents)


class ProjectFileTest(ConnectWriteTestCase):
    def test_vscode_creates_its_directory_in_the_working_directory(self) -> None:
        report = self.write_json("vscode", "--apply")
        target = self.project / ".vscode" / "mcp.json"
        self.assertEqual(report["write"]["path"], str(target))
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"servers": {"noetrail": self.entry("vscode")}},
        )

    def test_claude_code_writes_the_project_file(self) -> None:
        self.invoke("--client", "claude-code", "--write", "--apply")
        self.assertIn(
            "noetrail",
            json.loads((self.project / ".mcp.json").read_text())["mcpServers"],
        )

    def test_opencode_gets_its_schema_on_a_new_file_only(self) -> None:
        (self.home / ".config" / "opencode").mkdir(parents=True)
        self.invoke("--client", "opencode", "--write", "--apply")
        target = self.home / ".config" / "opencode" / "opencode.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(list(document), ["$schema", "mcp"])
        self.assertEqual(document["$schema"], "https://opencode.ai/config.json")
        self.assertEqual(document["mcp"]["noetrail"], self.entry("opencode"))

        existing = self.base / "existing.json"
        existing.write_text('{"theme": "dark"}\n', encoding="utf-8")
        self.invoke(
            "--client", "opencode", "--write", "--apply", "--config-file", str(existing)
        )
        self.assertEqual(
            list(json.loads(existing.read_text(encoding="utf-8"))), ["theme", "mcp"]
        )


class OptionsAndApplyTest(ConnectWriteTestCase):
    def test_write_options_need_write(self) -> None:
        for extra in (["--apply"], ["--force"], ["--config-file", "x.json"]):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(InvalidRequest, "only applies with --write"),
            ):
                run_command(["connect", "--client", "cursor", *extra])
        with self.assertRaisesRegex(InvalidRequest, "--write needs --client"):
            run_command(["connect", "--write"])

    def test_only_apply_declares_a_write(self) -> None:
        parser = build_parser()
        for arguments, expected in (
            (["connect", "--client", "cursor"], False),
            (["connect", "--client", "cursor", "--write"], False),
            (["connect", "--client", "cursor", "--write", "--apply"], True),
        ):
            with self.subTest(arguments=arguments):
                self.assertIs(
                    command_is_mutating(parser.parse_args(arguments)), expected
                )

    def plan(self) -> connect_write.WritePlan:
        host = connect_module.find_host("cursor")
        layout = resolve_layout(data_root=self.data, config_root=self.config)
        report = connect_module.connection(
            host, "console", layout, explicit_builtins=False
        )
        args = build_parser().parse_args(["connect", "--client", "cursor", "--write"])
        return connect_write.plan_write(
            host, report, layout, args, platform="linux", cwd=self.project
        )

    def test_a_file_changed_after_the_preview_is_not_overwritten(self) -> None:
        self.make_cursor_file('{"mcpServers": {}}\n')
        plan = self.plan()
        self.cursor_file.write_text('{"edited": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(Conflict, "changed while connect"):
            connect_write.apply(plan)
        self.assertEqual(
            self.cursor_file.read_text(encoding="utf-8"), '{"edited": true}\n'
        )
        self.assertFalse(plan.applied)

    def test_a_refused_plan_applies_nothing(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.action, "refuse")
        connect_write.apply(plan)
        self.assertFalse(plan.applied)

    def test_a_failed_write_is_a_storage_error(self) -> None:
        (self.home / ".cursor").mkdir()
        with (
            patch.object(connect_write, "atomic_write", side_effect=OSError("full")),
            self.assertRaisesRegex(StorageError, "Could not write"),
        ):
            run_command(["connect", "--client", "cursor", "--write", "--apply"])
        self.assertFalse(self.cursor_file.exists())


class SubprocessTest(unittest.TestCase):
    def test_preview_apply_and_refusal_from_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            home = base / "home"
            (home / ".gemini").mkdir(parents=True)
            (home / "noetrail" / "data").mkdir(parents=True)
            neutral = base / "neutral"
            neutral.mkdir()
            environment = without_root_variables()
            environment["HOME"] = str(home)

            def run(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*CLI_COMMAND, "connect", "--client", *arguments],
                    text=True,
                    capture_output=True,
                    check=False,
                    cwd=neutral,
                    env=environment,
                )

            target = home / ".gemini" / "settings.json"
            preview = run("gemini", "--write")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("--apply", preview.stdout)
            self.assertFalse(target.exists())

            applied = run("gemini", "--write", "--apply", "--json")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertTrue(json.loads(applied.stdout)["write"]["applied"])
            self.assertIn(
                "noetrail", json.loads(target.read_text())["mcpServers"]
            )

            refused = run("codex", "--write")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("TOML", refused.stderr)
            self.assertEqual(list(neutral.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

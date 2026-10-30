"""`noetrail connect`: the configuration block for one known MCP host."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from noetrail.cli import run_command
from noetrail.commands import connect as connect_module
from noetrail.errors import ConfigurationError, InvalidRequest
from noetrail.layout import resolve_layout
from noetrail.version import application_version
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = (REPOSITORY_ROOT / ".knowledge").resolve()
CLIENTS = ("claude-code", "claude-desktop", "codex", "zeroclaw")
LAUNCHERS = ("console", "module", "uvx")
ROOT_VARIABLES = (
    "NOETRAIL_DATA_ROOT",
    "NOETRAIL_CONFIG_ROOT",
    "NOETRAIL_BUILTINS_ROOT",
    "KNOWLEDGE_DATA_ROOT",
    "KNOWLEDGE_CONFIG_ROOT",
    "KNOWLEDGE_BUILTINS_ROOT",
)
REPORT_KEYS = {
    "client",
    "config_path",
    "format",
    "launcher",
    "server",
    "snippet",
    "skills_dir",
    "skills_export",
    "claude_mcp_add",
}


def without_root_variables() -> dict[str, str]:
    return {
        name: value for name, value in os.environ.items() if name not in ROOT_VARIABLES
    }


def server_from_snippet(client: str, snippet: str) -> dict[str, object]:
    """Parse the pasted block the way the host would and return the server."""

    if client in {"claude-code", "claude-desktop"}:
        server = json.loads(snippet)["mcpServers"]["noetrail"]
    elif client == "codex":
        server = tomllib.loads(snippet)["mcp_servers"]["noetrail"]
    else:
        servers = tomllib.loads(snippet)["mcp"]["servers"]
        assert len(servers) == 1
        server = servers[0]
    assert isinstance(server, dict)
    return server


class ConnectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.home = self.base / "home"
        self.home.mkdir()
        self.instance = self.home / "noetrail"
        self.data = self.instance / "data"
        self.config = self.instance / "config"
        self.data.mkdir(parents=True)
        self.config.mkdir()
        environment = without_root_variables()
        environment["HOME"] = str(self.home)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run_command(list(arguments))
        self.assertEqual(status, 0)
        return output.getvalue()

    def connect(self, *arguments: str) -> dict[str, object]:
        report = json.loads(
            self.run_cli(
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                "connect",
                "--json",
                *arguments,
            )
        )
        self.assertIsInstance(report, dict)
        return dict(report)


class EveryClientAndLauncherTest(ConnectTestCase):
    def test_every_combination_parses_and_carries_the_absolute_roots(self) -> None:
        for client in CLIENTS:
            for launcher in LAUNCHERS:
                with self.subTest(client=client, launcher=launcher):
                    report = self.connect("--client", client, "--launcher", launcher)
                    self.assertEqual(set(report), REPORT_KEYS)
                    self.assertEqual(report["client"], client)
                    self.assertEqual(report["launcher"], launcher)
                    snippet = report["snippet"]
                    assert isinstance(snippet, str)
                    server = server_from_snippet(client, snippet)
                    self.assertEqual(server, report["server"])
                    arguments = server["args"]
                    assert isinstance(arguments, list)
                    data_index = arguments.index("--data-root")
                    config_index = arguments.index("--config-root")
                    self.assertEqual(arguments[data_index + 1], str(self.data))
                    self.assertEqual(arguments[config_index + 1], str(self.config))
                    for value in (self.data, self.config):
                        self.assertTrue(value.is_absolute())

    def test_the_text_form_names_the_host_and_contains_the_block(self) -> None:
        for client in CLIENTS:
            for launcher in LAUNCHERS:
                with self.subTest(client=client, launcher=launcher):
                    text = self.run_cli(
                        "--data-root",
                        str(self.data),
                        "--config-root",
                        str(self.config),
                        "connect",
                        "--client",
                        client,
                        "--launcher",
                        launcher,
                    )
                    report = self.connect("--client", client, "--launcher", launcher)
                    self.assertIn(f"({client})", text)
                    self.assertIn(str(report["snippet"]), text)
                    self.assertIn("docs/integrations/mcp-clients.md", text)

    def test_the_formats_match_the_documented_hosts(self) -> None:
        formats = {
            client: self.connect("--client", client)["format"] for client in CLIENTS
        }
        self.assertEqual(
            formats,
            {
                "claude-code": "json",
                "claude-desktop": "json",
                "codex": "toml",
                "zeroclaw": "toml",
            },
        )


class LauncherTest(ConnectTestCase):
    def test_console_runs_the_installed_command(self) -> None:
        server = self.connect("--client", "claude-desktop")["server"]
        self.assertEqual(
            server,
            {
                "command": "noetrail-mcp",
                "args": [
                    "--data-root",
                    str(self.data),
                    "--config-root",
                    str(self.config),
                ],
            },
        )

    def test_module_uses_the_running_interpreter(self) -> None:
        server = self.connect("--client", "codex", "--launcher", "module")["server"]
        assert isinstance(server, dict)
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"][:2], ["-m", "noetrail.mcp"])

    def test_uvx_pins_the_installed_version(self) -> None:
        server = self.connect("--client", "claude-code", "--launcher", "uvx")["server"]
        assert isinstance(server, dict)
        self.assertEqual(server["command"], "uvx")
        self.assertEqual(
            server["args"][:3],
            ["--from", f"noetrail=={application_version()}", "noetrail-mcp"],
        )

    def test_an_unknown_launcher_is_refused(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "console, module, uvx"):
            connect_module.launcher_argv("npx")

    def test_an_explicit_builtins_root_is_passed_on(self) -> None:
        report = json.loads(
            self.run_cli(
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                "--builtins-root",
                str(BUILTINS_ROOT),
                "connect",
                "--client",
                "codex",
                "--json",
            )
        )
        self.assertEqual(
            report["server"]["args"][-2:], ["--builtins-root", str(BUILTINS_ROOT)]
        )


class ClaudeCodeTest(ConnectTestCase):
    def test_the_command_line_form_is_the_same_server(self) -> None:
        report = self.connect("--client", "claude-code", "--launcher", "module")
        line = report["claude_mcp_add"]
        assert isinstance(line, str)
        words = shlex.split(line)
        self.assertEqual(
            words[:7], ["claude", "mcp", "add", "--scope", "user", "noetrail", "--"]
        )
        server = report["server"]
        assert isinstance(server, dict)
        self.assertEqual(words[7:], [server["command"], *server["args"]])
        self.assertEqual(report["skills_dir"], "~/.claude/skills")
        self.assertEqual(
            report["skills_export"], "noetrail skills export ~/.claude/skills"
        )

    def test_a_root_with_spaces_survives_shell_quoting(self) -> None:
        spaced = self.base / "with space" / "data"
        spaced.mkdir(parents=True)
        report = json.loads(
            self.run_cli(
                "--data-root",
                str(spaced),
                "--config-root",
                str(self.config),
                "connect",
                "--client",
                "claude-code",
                "--json",
            )
        )
        self.assertIn(str(spaced), shlex.split(report["claude_mcp_add"]))

    def test_other_hosts_have_no_command_line_form(self) -> None:
        for client in ("claude-desktop", "codex", "zeroclaw"):
            with self.subTest(client=client):
                self.assertIsNone(self.connect("--client", client)["claude_mcp_add"])


class HostTableTest(ConnectTestCase):
    def test_claude_desktop_has_no_skills_and_a_platform_path(self) -> None:
        host = connect_module.find_host("claude-desktop")
        layout = resolve_layout(data_root=self.data, config_root=self.config)
        paths = {
            system: connect_module.connection(
                host, "console", layout, explicit_builtins=False, system=system
            )["config_path"]
            for system in ("darwin", "linux", "win32")
        }
        self.assertEqual(
            paths,
            {
                "darwin": (
                    "~/Library/Application Support/Claude/claude_desktop_config.json"
                ),
                "linux": None,
                "win32": r"%APPDATA%\Claude\claude_desktop_config.json",
            },
        )
        report = self.connect("--client", "claude-desktop")
        self.assertIsNone(report["skills_dir"])
        self.assertIsNone(report["skills_export"])

    def test_a_platform_without_a_path_says_so_in_text(self) -> None:
        host = connect_module.find_host("claude-desktop")
        layout = resolve_layout(data_root=self.data, config_root=self.config)
        report = connect_module.connection(
            host, "console", layout, explicit_builtins=False, system="linux"
        )
        text = connect_module.render_text(host, report)
        self.assertIn("no documented location on this platform", text)
        self.assertIn("Add this to Claude Desktop's configuration:", text)
        self.assertNotIn("Skills:", text)

    def test_platform_names(self) -> None:
        self.assertEqual(connect_module.platform_name("darwin"), "macos")
        self.assertEqual(connect_module.platform_name("win32"), "windows")
        self.assertEqual(connect_module.platform_name("linux"), "linux")
        self.assertIn(connect_module.platform_name(), connect_module.PLATFORMS)

    def test_every_host_names_a_path_or_none_for_every_platform(self) -> None:
        for host in connect_module.HOSTS:
            with self.subTest(host=host.name):
                self.assertEqual(
                    set(host.config_paths), set(connect_module.PLATFORMS)
                )
                for path in host.config_paths.values():
                    # Printed as written; nothing is expanded for the reader.
                    if path is not None:
                        self.assertNotIn(str(self.home), path)

    def test_codex_uses_its_own_table_and_skills_directory(self) -> None:
        report = self.connect("--client", "codex")
        self.assertEqual(report["config_path"], "~/.codex/config.toml")
        self.assertEqual(report["skills_dir"], "~/.codex/skills")
        snippet = report["snippet"]
        assert isinstance(snippet, str)
        self.assertTrue(snippet.startswith("[mcp_servers.noetrail]\n"))


class ZeroClawShapeTest(ConnectTestCase):
    """The block keeps the shape of the deploy example, roots substituted."""

    def deploy_example_server(self) -> dict[str, object]:
        example = tomllib.loads(
            (
                REPOSITORY_ROOT / "deploy/zeroclaw/security-profiles.example.toml"
            ).read_text(encoding="utf-8")
        )
        (server,) = [
            entry for entry in example["mcp"]["servers"] if entry["name"] == "knowledge"
        ]
        assert isinstance(server, dict)
        return server

    def test_module_launcher_reproduces_the_deploy_example(self) -> None:
        example = self.deploy_example_server()
        substitutions = {
            "PYTHONPATH=/srv/noetrail/src": f"PYTHONPATH={REPOSITORY_ROOT / 'src'}",
            "python3": sys.executable,
            "/srv/noetrail-data": str(self.data),
            "/etc/noetrail": str(self.config),
            "/srv/noetrail/.knowledge": str(BUILTINS_ROOT),
        }
        expected = dict(example)
        expected["args"] = [
            substitutions.get(value, value) for value in example["args"]
        ]
        report = self.connect("--client", "zeroclaw", "--launcher", "module")
        snippet = report["snippet"]
        assert isinstance(snippet, str)
        self.assertEqual(server_from_snippet("zeroclaw", snippet), expected)

    def test_console_and_uvx_keep_the_wrapper_and_drop_pythonpath(self) -> None:
        example = self.deploy_example_server()
        environment = [
            value
            for value in example["args"][1:]
            if "=" in str(value) and not str(value).startswith("PYTHONPATH=")
        ]
        for launcher, command in (
            ("console", ["noetrail-mcp"]),
            (
                "uvx",
                ["uvx", "--from", f"noetrail=={application_version()}", "noetrail-mcp"],
            ),
        ):
            with self.subTest(launcher=launcher):
                server = self.connect("--client", "zeroclaw", "--launcher", launcher)[
                    "server"
                ]
                assert isinstance(server, dict)
                self.assertEqual(server["command"], example["command"])
                self.assertEqual(
                    server["tool_timeout_secs"], example["tool_timeout_secs"]
                )
                arguments = server["args"]
                self.assertEqual(arguments[0], "-i")
                self.assertEqual(
                    arguments[1 : 1 + len(environment)], environment
                )
                start = 1 + len(environment)
                self.assertEqual(
                    arguments[start : start + len(command)], command
                )
                self.assertEqual(
                    "--builtins-root" in arguments, launcher == "console"
                )

    def test_the_skills_directory_comes_from_the_setup_guide(self) -> None:
        report = self.connect("--client", "zeroclaw")
        self.assertEqual(
            report["skills_dir"], "<zeroclaw-install>/shared/skills/knowledge-vault"
        )
        text = self.run_cli(
            "--data-root",
            str(self.data),
            "--config-root",
            str(self.config),
            "connect",
            "--client",
            "zeroclaw",
        )
        self.assertIn("docs/integrations/zeroclaw-setup.md", text)
        self.assertIn("Merge this into ~/.zeroclaw/config.toml:", text)


class TomlStringTest(unittest.TestCase):
    def test_round_trips_through_the_standard_reader(self) -> None:
        for value in (
            "plain",
            'a "quoted" name',
            "C:\\Users\\example\\noetrail",
            "tab\tnew\nline\rfeed\fback\bspace",
            "bell\x07 delete\x7f escape\x1b",
            "ümlaut and 日本語",
            "",
        ):
            with self.subTest(value=value):
                parsed = tomllib.loads(f"key = {connect_module.toml_string(value)}")
                self.assertEqual(parsed["key"], value)

    def test_arrays_round_trip(self) -> None:
        values = ["-i", 'x="y"', "back\\slash"]
        parsed = tomllib.loads(f"key = {connect_module.toml_array(values)}")
        self.assertEqual(parsed["key"], values)

    def test_an_undecodable_path_is_refused(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "not valid Unicode"):
            connect_module.toml_string("bad\udcff")


class ListingAndErrorsTest(ConnectTestCase):
    def test_without_a_client_it_lists_the_supported_ones(self) -> None:
        text = self.run_cli("connect")
        for client in CLIENTS:
            self.assertIn(client, text)
        listing = json.loads(self.run_cli("connect", "--json"))
        self.assertEqual(
            [entry["client"] for entry in listing["clients"]], list(CLIENTS)
        )

    def test_an_unknown_client_names_the_supported_ones(self) -> None:
        with self.assertRaises(InvalidRequest) as raised:
            run_command(["connect", "--client", "notepad"])
        for client in CLIENTS:
            self.assertIn(client, raised.exception.message)

    def test_the_quickstart_default_instance_is_found_without_options(self) -> None:
        report = json.loads(self.run_cli("connect", "--client", "codex", "--json"))
        self.assertEqual(
            report["server"]["args"],
            ["--data-root", str(self.data), "--config-root", str(self.config)],
        )

    def test_path_selects_a_quickstart_instance(self) -> None:
        other = self.base / "elsewhere"
        (other / "data").mkdir(parents=True)
        report = json.loads(
            self.run_cli(
                "connect", "--client", "codex", "--path", str(other), "--json"
            )
        )
        self.assertEqual(report["server"]["args"][1], str(other / "data"))
        self.assertFalse((other / "config").exists())

    def test_path_cannot_be_combined_with_root_options(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "--path cannot be combined"):
            run_command(
                [
                    "--data-root",
                    str(self.data),
                    "connect",
                    "--client",
                    "codex",
                    "--path",
                    str(self.instance),
                ]
            )

    def test_the_environment_root_wins_over_the_default(self) -> None:
        other = self.base / "environment-data"
        other.mkdir()
        with patch.dict(os.environ, {"NOETRAIL_DATA_ROOT": str(other)}):
            report = json.loads(self.run_cli("connect", "--client", "codex", "--json"))
        self.assertEqual(report["server"]["args"][1], str(other))

    def test_a_missing_instance_points_at_quickstart_and_creates_nothing(
        self,
    ) -> None:
        missing = self.base / "missing"
        with self.assertRaisesRegex(ConfigurationError, "noetrail quickstart"):
            run_command(["connect", "--client", "codex", "--path", str(missing)])
        self.assertFalse(missing.exists())

    def test_the_legacy_combined_root_is_passed_as_root(self) -> None:
        combined = self.base / "combined"
        (combined / ".knowledge").mkdir(parents=True)
        for name in ("SPEC.md", "relation-types.yaml"):
            (combined / ".knowledge" / name).write_bytes(
                (BUILTINS_ROOT / name).read_bytes()
            )
        report = json.loads(
            self.run_cli(
                "--root", str(combined), "connect", "--client", "codex", "--json"
            )
        )
        self.assertEqual(report["server"]["args"], ["--root", str(combined)])

    def test_connect_writes_nothing_into_the_instance(self) -> None:
        before = sorted(self.instance.rglob("*"))
        for client in CLIENTS:
            self.run_cli("connect", "--client", client)
        self.assertEqual(sorted(self.instance.rglob("*")), before)


class QuickstartClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.instance = self.base / "instance"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def quickstart(self, *extra: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = run_command(
                ["quickstart", "--path", str(self.instance), *extra]
            )
        self.assertEqual(status, 0)
        return output.getvalue()

    def test_text_prints_the_client_block_instead_of_the_generic_one(self) -> None:
        text = self.quickstart("--client", "codex")
        self.assertIn("[mcp_servers.noetrail]", text)
        self.assertNotIn('"mcpServers"', text)
        self.assertIn(str(self.instance / "data"), text)

    def test_json_keeps_mcp_config_and_adds_connect(self) -> None:
        report = json.loads(self.quickstart("--client", "zeroclaw", "--json"))
        self.assertIn("mcpServers", report["mcp_config"])
        self.assertEqual(set(report["connect"]), REPORT_KEYS)
        self.assertEqual(report["connect"]["client"], "zeroclaw")
        server = server_from_snippet("zeroclaw", report["connect"]["snippet"])
        self.assertIn(report["data_root"], server["args"])

    def test_without_a_client_connect_is_null_and_the_hint_is_printed(self) -> None:
        report = json.loads(self.quickstart("--json"))
        self.assertIsNone(report["connect"])
        self.assertIn("connect --client NAME", self.quickstart())

    def test_an_unknown_client_is_refused_before_anything_is_created(self) -> None:
        with self.assertRaises(InvalidRequest):
            run_command(
                ["quickstart", "--path", str(self.instance), "--client", "notepad"]
            )
        self.assertFalse(self.instance.exists())


class SubprocessTest(unittest.TestCase):
    def test_connect_runs_from_a_neutral_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            data = base / "data"
            config = base / "config"
            data.mkdir()
            neutral = base / "neutral"
            neutral.mkdir()
            environment = without_root_variables()
            environment["HOME"] = str(base / "home")
            result = subprocess.run(
                [
                    *CLI_COMMAND,
                    "--data-root",
                    str(data),
                    "--config-root",
                    str(config),
                    "connect",
                    "--client",
                    "claude-code",
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=False,
                cwd=neutral,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            server = server_from_snippet("claude-code", report["snippet"])
            self.assertEqual(
                server["args"],
                ["--data-root", str(data), "--config-root", str(config)],
            )
            self.assertFalse(config.exists())
            self.assertEqual(list(neutral.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

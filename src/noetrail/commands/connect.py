#!/usr/bin/env python3
"""The `connect` command: the configuration one known MCP host expects.

`quickstart` prints one generic `mcpServers` block, and most hosts accept
something like it, but not where or in the form a reader would guess: Claude
Code keeps it in `.mcp.json` or behind `claude mcp add`, Claude Desktop in a
platform-specific application directory, Codex in TOML under a different key,
VS Code under `servers` with a required `type`, OpenCode under `mcp` with the
command as one array, and ZeroClaw in TOML wrapped in `env -i`. Each
difference is one more thing to look up before the first answer.

`connect` prints the block for a named host with this instance's absolute
roots, where the host reads it from, and where the host loads skills from. By
default it writes nothing: not the host's configuration, not the instance, not
a lock file. `--write --apply` merges the entry into a JSON host's file, see
`connect_write`. Everything it knows about a host is in `HOSTS`, so adding one
is a table entry and a test, not a new code path.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import sys

from noetrail.commands import connect_write
from noetrail.errors import ConfigurationError, InvalidRequest
from noetrail.layout import (
    LayoutError,
    NoetrailLayout,
    environment_builtins_root,
    environment_config_root,
    environment_data_root,
    resolve_layout,
)
from noetrail.version import DISTRIBUTION_NAME, application_version

# Where `quickstart` creates an instance when it is not given `--path`, so
# `connect` finds that instance without being told where it is.
DEFAULT_INSTANCE_DIRECTORY = "noetrail"

LAUNCHERS = ("console", "module", "uvx")
PLATFORMS = ("macos", "linux", "windows")
MCP_CLIENTS_DOCUMENT = "docs/integrations/mcp-clients.md"

# The environment the ZeroClaw deploy example starts the server with. ZeroClaw
# stdio children inherit the daemon's environment, provider secrets included;
# `env -i` with exactly these variables keeps them out of the Python process.
ZEROCLAW_ENVIRONMENT = (
    "PATH=/usr/local/bin:/usr/bin:/bin",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "PYTHONIOENCODING=utf-8",
    "PYTHONUNBUFFERED=1",
    "PYTHONUTF8=1",
)
ZEROCLAW_TOOL_TIMEOUT_SECONDS = 30

# The user-level skills directory several hosts read besides their own, so a
# single `noetrail skills export` there reaches all of them.
SHARED_SKILLS_DIR = "~/.agents/skills"


@dataclass(frozen=True)
class Host:
    """What one MCP host needs to know to start the Noetrail server.

    ``shape`` names how the server entry is laid out: ``mcp-servers-json`` is
    ``{TOP_LEVEL_KEY: {NAME: {"command", "args"}}}``, with ``"type":
    SERVER_TYPE`` first when the host requires one; ``opencode-json`` is
    ``{TOP_LEVEL_KEY: {NAME: {"type", "command": [COMMAND, ARGS...],
    "enabled"}}}``; ``codex-toml`` is a ``[mcp_servers.NAME]`` table with
    ``command`` and ``args``; and ``zeroclaw-toml`` is one ``[[mcp.servers]]``
    element in the shape of ``deploy/zeroclaw/security-profiles.example.toml``.
    ``json_schema``, when set, is the ``$schema`` the host's examples put at
    the top of a JSON file.

    ``add_command`` is the host's own command for registering a stdio server.
    ``add_style`` says what follows it: ``separator`` is ``NAME -- COMMAND
    ARGS...``, ``positional`` is ``NAME COMMAND ARGS...`` for a host without a
    ``--`` separator, and ``json`` is one argument holding the server entry as
    compact JSON with its ``name``.

    ``other_skills_dirs`` are further user-level directories the host loads
    skills from; ``skills_dir`` is the one ``connect`` recommends.
    """

    name: str
    display_name: str
    config_paths: Mapping[str, str | None]
    config_format: str
    top_level_key: str
    shape: str
    server_name: str
    skills_dir: str | None
    config_hint: str = ""
    server_type: str | None = None
    json_schema: str | None = None
    add_command: tuple[str, ...] = ()
    add_style: str = "separator"
    add_command_hint: str = ""
    other_skills_dirs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


HOSTS: tuple[Host, ...] = (
    # Source: Anthropic, Claude Code documentation, "Connect Claude Code to
    # tools via MCP" (project scope `.mcp.json`, `claude mcp add --scope
    # user`) and "Agent Skills" (`~/.claude/skills/<name>/SKILL.md`), as read
    # on 2026-10-30.
    Host(
        name="claude-code",
        display_name="Claude Code",
        config_paths={
            "macos": ".mcp.json",
            "linux": ".mcp.json",
            "windows": ".mcp.json",
        },
        config_format="json",
        top_level_key="mcpServers",
        shape="mcp-servers-json",
        server_name="noetrail",
        skills_dir="~/.claude/skills",
        config_hint=(
            "in the project root, or user scope through `claude mcp add`"
        ),
        add_command=("claude", "mcp", "add", "--scope", "user"),
        add_command_hint="Or register it for every project from a shell:",
    ),
    # Source: Anthropic, "Getting started with local MCP servers on Claude
    # Desktop" and the Model Context Protocol "Connect to local MCP servers"
    # guide (`claude_desktop_config.json` paths per platform), as read on
    # 2026-10-30. There is no Linux build to point at.
    Host(
        name="claude-desktop",
        display_name="Claude Desktop",
        config_paths={
            "macos": (
                "~/Library/Application Support/Claude/claude_desktop_config.json"
            ),
            "linux": None,
            "windows": r"%APPDATA%\Claude\claude_desktop_config.json",
        },
        config_format="json",
        top_level_key="mcpServers",
        shape="mcp-servers-json",
        server_name="noetrail",
        skills_dir=None,
        notes=("Restart Claude Desktop after changing its configuration.",),
    ),
    # Source: OpenAI, Codex documentation, as read on 2026-10-30.
    # - "Model Context Protocol" (https://developers.openai.com/codex/mcp,
    #   redirecting to https://learn.chatgpt.com/docs/extend/mcp?surface=cli):
    #   `[mcp_servers.<name>]` with `command` and `args` in
    #   `~/.codex/config.toml`, or `.codex/config.toml` in a trusted project;
    #   `codex mcp add <server-name> -- <stdio server-command>`.
    # - "Agent Skills" (https://developers.openai.com/codex/skills,
    #   redirecting to https://learn.chatgpt.com/docs/build-skills): user
    #   skills in `$HOME/.agents/skills`; repository skills in `.agents/skills`
    #   in the current directory, a parent inside the repository, or the
    #   repository root; admin-wide skills in `/etc/codex/skills`. The skills
    #   here carry Codex's `agents/openai.yaml` overlay.
    # A `$CODEX_HOME` override is not printed: neither page nor the Codex
    # repository's `docs/config.md` (checked the same day) mentions it.
    Host(
        name="codex",
        display_name="Codex CLI",
        config_paths={
            "macos": "~/.codex/config.toml",
            "linux": "~/.codex/config.toml",
            "windows": "~/.codex/config.toml",
        },
        config_format="toml",
        top_level_key="mcp_servers",
        shape="codex-toml",
        server_name="noetrail",
        skills_dir="~/.agents/skills",
        config_hint="or .codex/config.toml in a trusted project",
        add_command=("codex", "mcp", "add"),
        add_command_hint="Or add it from a shell:",
        notes=(
            "A repository-level .agents/skills directory works for skills "
            "too.",
        ),
    ),
    # Source: Cursor documentation, as read on 2026-10-30.
    # - "Model Context Protocol (MCP)" (https://cursor.com/docs/context/mcp):
    #   `.cursor/mcp.json` in a project, `~/.cursor/mcp.json` globally, on
    #   every platform; `{"mcpServers": {NAME: {"type", "command", "args"}}}`.
    #   The field table lists `type: "stdio"` as required although the
    #   examples omit it, so it is printed. There is no command-line form.
    # - "Agent Skills" (https://cursor.com/docs/context/skills): project
    #   `.agents/skills/` or `.cursor/skills/`, user `~/.agents/skills/` or
    #   `~/.cursor/skills/`, and for compatibility `.claude/skills`,
    #   `.codex/skills`, `~/.claude/skills` and `~/.codex/skills`.
    Host(
        name="cursor",
        display_name="Cursor",
        config_paths={
            "macos": "~/.cursor/mcp.json",
            "linux": "~/.cursor/mcp.json",
            "windows": "~/.cursor/mcp.json",
        },
        config_format="json",
        top_level_key="mcpServers",
        shape="mcp-servers-json",
        server_name="noetrail",
        skills_dir="~/.agents/skills",
        config_hint="or .cursor/mcp.json in a project",
        server_type="stdio",
        other_skills_dirs=("~/.cursor/skills", "~/.claude/skills", "~/.codex/skills"),
        notes=(
            "A project-level .agents/skills or .cursor/skills directory works "
            "for skills too.",
        ),
    ),
    # Source: Gemini CLI documentation, as read on 2026-10-30.
    # - "MCP servers with the Gemini CLI"
    #   (https://geminicli.com/docs/tools/mcp-server/): `mcpServers` with
    #   `command` and `args` in `~/.gemini/settings.json` or a project's
    #   `.gemini/settings.json`; the optional `timeout` and `trust` are left
    #   unset; server names should avoid underscores, which `noetrail` does.
    #   `gemini mcp add [options] <name> <commandOrUrl> [args...]` with
    #   `-s, --scope user|project` (default project) and no `--` separator.
    # - "Agent Skills" (https://geminicli.com/docs/cli/skills/): user
    #   `~/.gemini/skills/` or the alias `~/.agents/skills/`, workspace
    #   `.gemini/skills/` or `.agents/skills/`; within a tier `.agents/skills`
    #   takes precedence.
    # The same page says Gemini CLI was replaced by Antigravity CLI for
    # unpaid-tier and Google One users on 2026-06-18; the client keeps the
    # name `gemini`, which is the command it documents.
    Host(
        name="gemini",
        display_name="Gemini CLI",
        config_paths={
            "macos": "~/.gemini/settings.json",
            "linux": "~/.gemini/settings.json",
            "windows": "~/.gemini/settings.json",
        },
        config_format="json",
        top_level_key="mcpServers",
        shape="mcp-servers-json",
        server_name="noetrail",
        skills_dir="~/.agents/skills",
        config_hint="or .gemini/settings.json in a project",
        add_command=("gemini", "mcp", "add", "--scope", "user"),
        add_style="positional",
        add_command_hint="Or add it for your user from a shell:",
        other_skills_dirs=("~/.gemini/skills",),
        notes=(
            "A workspace-level .agents/skills or .gemini/skills directory "
            "works for skills too; within a level, .agents/skills takes "
            "precedence.",
        ),
    ),
    # Source: Visual Studio Code documentation, as read on 2026-10-30.
    # - "MCP configuration reference"
    #   (https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)
    #   and "Use MCP servers in VS Code"
    #   (https://code.visualstudio.com/docs/copilot/customization/mcp-servers):
    #   `.vscode/mcp.json` in a workspace, or the user profile's `mcp.json`
    #   through the Command Palette command "MCP: Open User Configuration",
    #   which has no stable path to print; `{"servers": {NAME: {"type":
    #   "stdio", "command", "args"}}}`; `code --add-mcp '<json>'` with one
    #   object carrying `name` adds it to the user profile.
    # - "Use Agent Skills in VS Code"
    #   (https://code.visualstudio.com/docs/copilot/customization/agent-skills):
    #   workspace `.github/skills/`, `.claude/skills/` and `.agents/skills/`;
    #   user `~/.copilot/skills/`, `~/.claude/skills/` and `~/.agents/skills/`.
    #   `~/.copilot/skills` is the directory Copilot itself reads.
    Host(
        name="vscode",
        display_name="VS Code",
        config_paths={
            "macos": ".vscode/mcp.json",
            "linux": ".vscode/mcp.json",
            "windows": ".vscode/mcp.json",
        },
        config_format="json",
        top_level_key="servers",
        shape="mcp-servers-json",
        server_name="noetrail",
        skills_dir="~/.copilot/skills",
        config_hint=(
            "in the workspace, or the user profile's mcp.json through the "
            "Command Palette: MCP: Open User Configuration"
        ),
        server_type="stdio",
        add_command=("code", "--add-mcp"),
        add_style="json",
        add_command_hint="Or add it to your user profile from a shell:",
        other_skills_dirs=("~/.agents/skills", "~/.claude/skills"),
        notes=(
            "~/.copilot/skills is the directory Copilot itself reads; "
            "~/.agents/skills is the shared alternative. A workspace-level "
            ".github/skills, .claude/skills or .agents/skills directory works "
            "too.",
        ),
    ),
    # Source: OpenCode documentation, as read on 2026-10-30.
    # - "MCP servers" (https://opencode.ai/docs/mcp-servers/) and "Config"
    #   (https://opencode.ai/docs/config/): `opencode.json` (or
    #   `opencode.jsonc`) in the project root, `~/.config/opencode/opencode.json`
    #   globally, or a file named by `OPENCODE_CONFIG`; `{"mcp": {NAME:
    #   {"type": "local", "command": [COMMAND, ARGS...], "enabled": true}}}`.
    #   `$schema` is optional; every example carries it, so it is printed.
    #   There is no command-line form.
    # - "Agent Skills" (https://opencode.ai/docs/skills/): project
    #   `.opencode/skills/`, `.claude/skills/` and `.agents/skills/`; global
    #   `~/.config/opencode/skills/`, `~/.claude/skills/` and
    #   `~/.agents/skills/`.
    Host(
        name="opencode",
        display_name="OpenCode",
        config_paths={
            "macos": "~/.config/opencode/opencode.json",
            "linux": "~/.config/opencode/opencode.json",
            "windows": "~/.config/opencode/opencode.json",
        },
        config_format="json",
        top_level_key="mcp",
        shape="opencode-json",
        server_name="noetrail",
        skills_dir="~/.agents/skills",
        config_hint="or opencode.json in the project root",
        server_type="local",
        json_schema="https://opencode.ai/config.json",
        other_skills_dirs=("~/.config/opencode/skills", "~/.claude/skills"),
        notes=(
            "OPENCODE_CONFIG can name another configuration file. A "
            "project-level .opencode/skills, .claude/skills or .agents/skills "
            "directory works for skills too.",
        ),
    ),
    # Source: this repository's `deploy/zeroclaw/security-profiles.example.toml`
    # and `docs/integrations/zeroclaw-setup.md`, checked against ZeroClaw
    # v0.8.4 as recorded in `docs/integrations/zeroclaw-security.md`, as read
    # on 2026-10-30. The alias `knowledge` is what the deploy risk profile's
    # `knowledge__*` tool names are written against.
    Host(
        name="zeroclaw",
        display_name="ZeroClaw",
        config_paths={
            "macos": "~/.zeroclaw/config.toml",
            "linux": "~/.zeroclaw/config.toml",
            "windows": "~/.zeroclaw/config.toml",
        },
        config_format="toml",
        top_level_key="mcp.servers",
        shape="zeroclaw-toml",
        server_name="knowledge",
        skills_dir="<zeroclaw-install>/shared/skills/knowledge-vault",
        notes=(
            "`env -i` limits PATH to /usr/local/bin:/usr/bin:/bin; the "
            "launcher command must be found there, or use --launcher module "
            "for an absolute interpreter path.",
            "Bundles, risk profiles, the attachment inbox and which skills "
            "the bundle includes: docs/integrations/zeroclaw-setup.md.",
        ),
    ),
)

HOSTS_BY_NAME = {host.name: host for host in HOSTS}


def supported_clients() -> str:
    return ", ".join(host.name for host in HOSTS)


def find_host(name: str) -> Host:
    host = HOSTS_BY_NAME.get(name)
    if host is None:
        raise InvalidRequest(
            f"Unknown client {name!r}; supported clients: {supported_clients()}"
        )
    return host


def platform_name(system: str | None = None) -> str:
    current = sys.platform if system is None else system
    if current == "darwin":
        return "macos"
    if current.startswith("win"):
        return "windows"
    return "linux"


def launcher_argv(launcher: str) -> list[str]:
    """The command and leading arguments that start the server process."""

    if launcher == "console":
        return ["noetrail-mcp"]
    if launcher == "module":
        # The interpreter running this command has Noetrail importable, which
        # is the one interpreter a host is sure to be able to start it with.
        return [sys.executable, "-m", "noetrail.mcp"]
    if launcher == "uvx":
        # Pinned to what is installed here, so the host launches the version
        # this configuration was generated by and checked against.
        return [
            "uvx",
            "--from",
            f"{DISTRIBUTION_NAME}=={application_version()}",
            "noetrail-mcp",
        ]
    raise InvalidRequest(
        f"Unknown launcher {launcher!r}; supported launchers: "
        f"{', '.join(LAUNCHERS)}"
    )


def root_arguments(layout: NoetrailLayout, *, include_builtins: bool) -> list[str]:
    """The server's root options, absolute because a host has its own cwd."""

    if layout.compatibility_root is not None:
        return ["--root", str(layout.compatibility_root)]
    arguments = [
        "--data-root",
        str(layout.data_root),
        "--config-root",
        str(layout.config_root),
    ]
    if include_builtins:
        arguments += ["--builtins-root", str(layout.builtins_root)]
    return arguments


def checkout_source(layout: NoetrailLayout) -> Path | None:
    """The `src/` directory when this runs from a source checkout."""

    source = layout.application_root / "src"
    return source if (source / "noetrail" / "__init__.py").is_file() else None


def server_command(
    host: Host,
    launcher: str,
    layout: NoetrailLayout,
    *,
    explicit_builtins: bool,
) -> tuple[str, list[str]]:
    """The executable and arguments the host starts the server with."""

    command = launcher_argv(launcher)
    if host.shape != "zeroclaw-toml":
        return command[0], command[1:] + root_arguments(
            layout, include_builtins=explicit_builtins
        )

    # The deploy example names the built-ins root explicitly; under `uvx` the
    # package brings its own, so a local path is passed only when asked for.
    include_builtins = explicit_builtins or launcher != "uvx"
    environment = list(ZEROCLAW_ENVIRONMENT)
    source = checkout_source(layout)
    if launcher == "module" and source is not None:
        environment.append(f"PYTHONPATH={source}")
    return "/usr/bin/env", [
        "-i",
        *environment,
        *command,
        *root_arguments(layout, include_builtins=include_builtins),
    ]


def server_entry(host: Host, command: str, arguments: list[str]) -> dict[str, object]:
    """The server definition exactly as it goes into the host's file."""

    if host.shape == "opencode-json":
        return {
            "type": host.server_type,
            "command": [command, *arguments],
            "enabled": True,
        }
    if host.shape != "zeroclaw-toml":
        entry: dict[str, object] = {}
        if host.server_type is not None:
            entry["type"] = host.server_type
        entry["command"] = command
        entry["args"] = arguments
        return entry
    return {
        "name": host.server_name,
        "transport": "stdio",
        "command": command,
        "args": arguments,
        "tool_timeout_secs": ZEROCLAW_TOOL_TIMEOUT_SECONDS,
    }


_TOML_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def toml_string(value: str) -> str:
    """A TOML basic string; the standard library can read TOML, not write it."""

    escaped: list[str] = []
    for character in value:
        code = ord(character)
        if character in _TOML_ESCAPES:
            escaped.append(_TOML_ESCAPES[character])
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            # An undecodable byte in a path; TOML has no spelling for it.
            raise InvalidRequest(
                f"Cannot write {value!r} as TOML: it is not valid Unicode"
            )
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def toml_array(values: list[str]) -> str:
    lines = ["["]
    lines += [f"  {toml_string(value)}," for value in values]
    lines.append("]")
    return "\n".join(lines)


def render_snippet(host: Host, command: str, arguments: list[str]) -> str:
    """The block a reader pastes into the host's configuration file."""

    if host.config_format == "json":
        document: dict[str, object] = {}
        if host.json_schema is not None:
            document["$schema"] = host.json_schema
        document[host.top_level_key] = {
            host.server_name: server_entry(host, command, arguments)
        }
        return json.dumps(document, ensure_ascii=False, indent=2)
    if host.shape == "codex-toml":
        return "\n".join(
            [
                f"[{host.top_level_key}.{host.server_name}]",
                f"command = {toml_string(command)}",
                f"args = {toml_array(arguments)}",
            ]
        )
    return "\n".join(
        [
            f"[[{host.top_level_key}]]",
            f"name = {toml_string(host.server_name)}",
            'transport = "stdio"',
            f"command = {toml_string(command)}",
            f"args = {toml_array(arguments)}",
            f"tool_timeout_secs = {ZEROCLAW_TOOL_TIMEOUT_SECONDS}",
        ]
    )


def mcp_add_command(host: Host, command: str, arguments: list[str]) -> str | None:
    """The host's own registration command for the block (POSIX quoting)."""

    if not host.add_command:
        return None
    if host.add_style == "json":
        definition = {
            "name": host.server_name,
            **server_entry(host, command, arguments),
        }
        tail = [json.dumps(definition, ensure_ascii=False, separators=(",", ":"))]
    elif host.add_style == "positional":
        tail = [host.server_name, command, *arguments]
    else:
        tail = [host.server_name, "--", command, *arguments]
    return shlex.join([*host.add_command, *tail])


def spoken_list(names: list[str]) -> str:
    """``A, B and C``."""

    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def shared_skills_line(host: Host) -> str | None:
    """The one sentence saying which hosts share ``SHARED_SKILLS_DIR``."""

    sharing = [
        other.display_name
        for other in HOSTS
        if SHARED_SKILLS_DIR in (other.skills_dir, *other.other_skills_dirs)
    ]
    if host.display_name not in sharing:
        return None
    return (
        f"{spoken_list(sharing)} all read {SHARED_SKILLS_DIR}, so one "
        f"`noetrail skills export {SHARED_SKILLS_DIR}` serves them all."
    )


def connection(
    host: Host,
    launcher: str,
    layout: NoetrailLayout,
    *,
    explicit_builtins: bool,
    system: str | None = None,
) -> dict[str, object]:
    """Everything `connect --json` reports; the key set is fixed."""

    command, arguments = server_command(
        host, launcher, layout, explicit_builtins=explicit_builtins
    )
    return {
        "client": host.name,
        "config_path": host.config_paths[platform_name(system)],
        "format": host.config_format,
        "launcher": launcher,
        "server": server_entry(host, command, arguments),
        "snippet": render_snippet(host, command, arguments),
        "skills_dir": host.skills_dir,
        "skills_export": (
            f"noetrail skills export {host.skills_dir}"
            if host.skills_dir is not None
            else None
        ),
        "mcp_add_command": mcp_add_command(host, command, arguments),
    }


def render_text(host: Host, report: Mapping[str, object]) -> str:
    """The human form of one `connection` report."""

    config_path = report["config_path"]
    if config_path is None:
        location = "no documented location on this platform"
        target = f"{host.display_name}'s configuration"
    else:
        location = f"{config_path} {host.config_hint}".rstrip()
        target = str(config_path)
    verb = "Merge this into" if host.config_format == "toml" else "Add this to"
    lines = [
        f"{host.display_name} ({host.name})",
        f"  config file   {location}",
        f"  launcher      {report['launcher']}",
        "",
        f"{verb} {target}:",
        "",
        str(report["snippet"]),
    ]
    if report["mcp_add_command"] is not None:
        lines += [
            "",
            host.add_command_hint,
            "",
            f"  {report['mcp_add_command']}",
        ]
    if report["skills_dir"] is not None:
        lines += [
            "",
            f"Skills: {host.display_name} loads them from {report['skills_dir']}.",
            "Copy them there with:",
            "",
            f"  {report['skills_export']}",
        ]
        alternatives: list[str] = []
        if host.other_skills_dirs:
            alternatives.append(
                f"{host.display_name} also reads skills from "
                f"{spoken_list(list(host.other_skills_dirs))}."
            )
        shared = shared_skills_line(host)
        if shared is not None:
            alternatives.append(shared)
        if alternatives:
            lines += ["", *alternatives]
    if host.notes:
        lines.append("")
        lines += list(host.notes)
    lines += ["", f"More hosts and launchers: {MCP_CLIENTS_DOCUMENT}"]
    return "\n".join(lines)


def render_listing() -> str:
    width = max(len(host.name) for host in HOSTS)
    lines = ["Supported clients (noetrail connect --client NAME):", ""]
    lines += [f"  {host.name:<{width}}   {host.display_name}" for host in HOSTS]
    lines += ["", f"Any other MCP host: {MCP_CLIENTS_DOCUMENT}"]
    return "\n".join(lines)


def builtins_requested(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "builtins_root", None) is not None
        or environment_builtins_root() is not None
    )


def resolve_connect_layout(args: argparse.Namespace) -> NoetrailLayout:
    """Resolve the instance to connect to, reading it and creating nothing.

    Explicit root options and the root environment variables win, exactly as
    for every other command. Without any, the instance is the one `quickstart`
    creates, below `--path` or `~/noetrail`, so `connect` works straight after
    it.
    """

    named = [args.root, args.data_root, args.config_root]
    data_root = args.data_root
    config_root = args.config_root
    if args.path is not None:
        if any(value is not None for value in named):
            raise InvalidRequest(
                "--path cannot be combined with --root, --data-root, or "
                "--config-root"
            )
        base: Path | None = Path(args.path).expanduser()
    elif any(value is not None for value in named) or (
        environment_data_root() or environment_config_root()
    ):
        base = None
    else:
        base = Path.home() / DEFAULT_INSTANCE_DIRECTORY
    if base is not None:
        data_root = base / "data"
        config_root = base / "config"
    try:
        return resolve_layout(
            root=args.root,
            data_root=data_root,
            config_root=config_root,
            builtins_root=args.builtins_root,
        )
    except LayoutError as exc:
        raise ConfigurationError(
            f"{exc}; run `noetrail quickstart` first, or pass --path, "
            "--data-root and --config-root for an existing instance"
        ) from exc


def check_write_options(args: argparse.Namespace) -> None:
    if not args.write:
        given = [
            option
            for option, value in (
                ("--apply", args.apply),
                ("--config-file", args.config_file is not None),
                ("--force", args.force),
            )
            if value
        ]
        if given:
            raise InvalidRequest(f"{', '.join(given)} only applies with --write")
    elif args.client is None:
        raise InvalidRequest("--write needs --client NAME")


def command_connect(args: argparse.Namespace) -> int:
    check_write_options(args)
    if args.client is None:
        if args.json:
            print(
                json.dumps(
                    {
                        "clients": [
                            {
                                "client": host.name,
                                "display_name": host.display_name,
                                "format": host.config_format,
                            }
                            for host in HOSTS
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(render_listing())
        return 0

    host = find_host(args.client)
    layout = resolve_connect_layout(args)
    report = connection(
        host,
        args.launcher,
        layout,
        explicit_builtins=builtins_requested(args),
    )
    if not args.write:
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_text(host, report))
        return 0

    plan = connect_write.plan_write(
        host, report, layout, args, platform=platform_name(), cwd=Path.cwd()
    )
    if args.apply:
        connect_write.apply(plan)
    if args.json:
        print(
            json.dumps(
                {**report, "write": plan.as_dict()}, ensure_ascii=False, indent=2
            )
        )
    else:
        print(connect_write.render_plan(host, plan, report))
    # A refusal is shown first, so a preview can show what it refused.
    if plan.error is not None:
        raise plan.error
    return 0

#!/usr/bin/env python3
"""The `connect` command: the configuration one known MCP host expects.

`quickstart` prints one generic `mcpServers` block, and most hosts accept
something like it, but not where or in the form a reader would guess: Claude
Code keeps it in `.mcp.json` or behind `claude mcp add`, Claude Desktop in a
platform-specific application directory, Codex in TOML under a different key,
and ZeroClaw in TOML wrapped in `env -i`. Each difference is one more thing to
look up before the first answer.

`connect` prints the block for a named host with this instance's absolute
roots, where the host reads it from, and where the host loads skills from. It
writes nothing: not the host's configuration, not the instance, not a lock
file. Everything it knows about a host is in `HOSTS`, so adding one is a table
entry and a test, not a new code path.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import sys

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


@dataclass(frozen=True)
class Host:
    """What one MCP host needs to know to start the Noetrail server.

    ``shape`` names how the server entry is laid out: ``mcp-servers-json`` is
    ``{"mcpServers": {NAME: {"command", "args"}}}``, ``codex-toml`` is a
    ``[mcp_servers.NAME]`` table with ``command`` and ``args``, and
    ``zeroclaw-toml`` is one ``[[mcp.servers]]`` element in the shape of
    ``deploy/zeroclaw/security-profiles.example.toml``.
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
    # Source: OpenAI, Codex documentation, "Model Context Protocol"
    # (`[mcp_servers.<name>]` in `~/.codex/config.toml`) and "Agent Skills"
    # (`~/.codex/skills/<name>/`, with the `agents/openai.yaml` overlay the
    # skills here carry), as read on 2026-10-30.
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
        skills_dir="~/.codex/skills",
        config_hint="or $CODEX_HOME/config.toml when CODEX_HOME is set",
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

    if host.shape != "zeroclaw-toml":
        return {"command": command, "args": arguments}
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

    if host.shape == "mcp-servers-json":
        return json.dumps(
            {
                host.top_level_key: {
                    host.server_name: server_entry(host, command, arguments)
                }
            },
            ensure_ascii=False,
            indent=2,
        )
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


def claude_mcp_add(host: Host, command: str, arguments: list[str]) -> str:
    """The `claude mcp add` line equivalent to the JSON block (POSIX quoting)."""

    return shlex.join(
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            host.server_name,
            "--",
            command,
            *arguments,
        ]
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
        "claude_mcp_add": (
            claude_mcp_add(host, command, arguments)
            if host.name == "claude-code"
            else None
        ),
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
    if report["claude_mcp_add"] is not None:
        lines += [
            "",
            "Or register it for every project from a shell:",
            "",
            f"  {report['claude_mcp_add']}",
        ]
    if report["skills_dir"] is not None:
        lines += [
            "",
            f"Skills: {host.display_name} loads them from {report['skills_dir']}.",
            "Copy them there with:",
            "",
            f"  {report['skills_export']}",
        ]
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


def command_connect(args: argparse.Namespace) -> int:
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
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(host, report))
    return 0

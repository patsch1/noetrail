#!/usr/bin/env python3
"""Exercise a built Noetrail wheel through install, upgrade, and restore."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile

LEGACY_ENTRY = """---
id: "kn_88888888888888888888888888888888"
schema_version: 8
type: "note"
title: "Synthetic pre-upgrade note"
created_at: "2025-01-01T10:00:00+00:00"
updated_at: "2025-01-01T10:00:00+00:00"
status: "active"
sensitivity: "normal"
tags: ["release-test"]
relations: []
origin: "conversation"
---

This is synthetic schema-8 data used only by release acceptance.
"""


def run(
    *arguments: str,
    input_text: str | None = None,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(arguments),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(arguments)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def archive_data(data_root: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(data_root.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("release fixture unexpectedly contains a symlink")
            if path.is_file():
                archive.write(path, path.relative_to(data_root))


def restore_data(archive_path: Path, data_root: Path) -> None:
    if data_root.exists():
        shutil.rmtree(data_root)
    data_root.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(data_root)


# Names that were importable before the 0.10.0a1 package migration. An installed
# wheel must never reintroduce them, because they are generic enough to shadow
# unrelated projects in the same environment.
RESERVED_TOP_LEVEL_NAMES = (
    "bookmark_fetch_mcp",
    "know",
    "knowledge_layout",
    "knowledge_mcp",
    "knowledge_migrations",
    "knowledge_schema",
    "knowledge_version",
)


def verify_import_namespace(python: Path, neutral_directory: Path) -> None:
    probe = (
        "import importlib.util, sys\n"
        f"names = {RESERVED_TOP_LEVEL_NAMES!r}\n"
        "found = [n for n in names if importlib.util.find_spec(n) is not None]\n"
        "assert not found, f'reserved top-level modules installed: {found}'\n"
        "import noetrail.cli, noetrail.mcp, noetrail.bookmark_fetcher\n"
    )
    run(str(python), "-c", probe, cwd=neutral_directory)


def verify_mcp_retrieval(
    binary: Path, data: Path, config: Path, entry_id: str, neutral_directory: Path
) -> None:
    """Exercise the installed stdio server, without importing checkout code."""
    calls = [
        ("search", {"query": "SSA"}),
        ("retrieve", {"query": "Examples", "max_body_chars": 1000}),
        ("retrieve", {"query": "SSA", "max_body_chars": 8}),
        ("search", {"query": "unrecordedzzzz"}),
        ("search", {"query": "Examples", "type": "recipe"}),
        ("get_entry", {"id": entry_id}),
    ]
    requests = [
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "release-acceptance", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        *[
            {
                "jsonrpc": "2.0", "id": number, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            for number, (name, arguments) in enumerate(calls, 3)
        ],
    ]
    wire = "".join(json.dumps(request) + "\n" for request in requests)
    reference = None
    for mode in ([], ["--subprocess-reads"]):
        response = run(
            str(binary), "--data-root", str(data), "--config-root", str(config),
            *mode, input_text=wire, cwd=neutral_directory, timeout=60,
        )
        rows = [json.loads(line) for line in response.stdout.splitlines()]
        if [row.get("id") for row in rows] != list(range(1, 9)):
            raise RuntimeError("installed MCP returned incomplete stdio responses")
        if any("error" in row for row in rows):
            raise RuntimeError("installed MCP rejected an acceptance request")
        if rows[0]["result"]["protocolVersion"] != "2024-11-05":
            raise RuntimeError("installed MCP protocol negotiation failed")
        names = {tool["name"] for tool in rows[1]["result"]["tools"]}
        if not {"search", "retrieve", "get_entry"} <= names or names & {
            "shell", "purge", "migrate",
        }:
            raise RuntimeError("installed MCP tool boundary changed")
        if any(row["result"].get("isError") for row in rows[2:]):
            raise RuntimeError("installed MCP retrieval failed")
        payloads = [row["result"]["structuredContent"] for row in rows[2:]]
        alias, forms, bounded, absent, filtered, full = payloads
        for result in (alias, forms, bounded):
            if result["total"] != 1 or result["items"][0]["id"] != entry_id:
                raise RuntimeError("installed MCP lost the selected synthetic entry")
        item = forms["items"][0]
        if item.get("match_kind") != "word_form" or item["body_truncated"]:
            raise RuntimeError("installed MCP lost word-form candidate metadata")
        if "fictional book" not in item["body"]:
            raise RuntimeError("installed MCP did not return source evidence")
        item = bounded["items"][0]
        if not item["body_truncated"] or len(item["body"]) > 8:
            raise RuntimeError("installed MCP body limit was not explicit")
        if absent["total"] != 0 or absent["items"]:
            raise RuntimeError("installed MCP invented an absent search result")
        if filtered["total"] != 0 or filtered["without_type_filter"]["total"] != 1:
            raise RuntimeError("installed MCP lost the wrong-type diagnostic")
        if full["id"] != entry_id or full["aliases"] != ["SSA"]:
            raise RuntimeError("installed MCP full-entry read lost identity or alias")
        if reference is not None and payloads != reference:
            raise RuntimeError("installed MCP read modes disagree")
        reference = payloads


def main() -> int:
    parser = argparse.ArgumentParser(prog="release-acceptance")
    parser.add_argument("--wheel", required=True)
    args = parser.parse_args()
    wheel = Path(args.wheel).expanduser().resolve(strict=True)
    if wheel.suffix != ".whl":
        raise SystemExit("--wheel must reference one built wheel")

    with tempfile.TemporaryDirectory(prefix="noetrail-release-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        binary = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = binary / ("python.exe" if sys.platform == "win32" else "python")
        noetrail = binary / ("noetrail.exe" if sys.platform == "win32" else "noetrail")
        run(str(python), "-m", "pip", "install", "--no-deps", str(wheel))
        verify_import_namespace(python, root)
        current = run(
            str(python),
            "-c",
            "from noetrail.migrations import CURRENT_SCHEMA_VERSION as v;print(v)",
            cwd=root,
        ).stdout.strip()

        data = root / "data"
        config = root / "config"
        run(
            str(noetrail),
            "--data-root",
            str(data),
            "--config-root",
            str(config),
            "init",
        )
        raw = data / "imports" / "raw" / "markdown" / "reading-list.md"
        raw.parent.mkdir(parents=True)
        raw.write_text(
            "# Synthetic release reading list\n\nLocal-first systems.\n",
            encoding="utf-8",
        )
        base = [
            str(noetrail),
            "--data-root",
            str(data),
            "--config-root",
            str(config),
        ]
        import_preview = json.loads(
            run(*base, "import", "markdown", "--source", "markdown").stdout
        )
        if not import_preview["dry_run"] or len(import_preview["created"]) != 1:
            raise RuntimeError("installed-wheel Markdown import preview failed")
        run(*base, "import", "markdown", "--source", "markdown", "--apply")
        attributes = root / "book.json"
        attributes.write_text(
            json.dumps(
                {
                    "author": "Ada Example",
                    "reading_state": "wishlist",
                    "topics": ["local-first systems"],
                }
            ),
            encoding="utf-8",
        )
        book = json.loads(run(
            *base,
            "capture",
            "--type",
            "books/book",
            "--title",
            "The Small System Atlas",
            "--alias",
            "SSA",
            "--attributes-file",
            str(attributes),
            "--text",
            "A fictional book used only by release acceptance.",
        ).stdout)
        search = json.loads(run(*base, "search", "local-first").stdout)
        if not any(
            item["title"] == "The Small System Atlas"
            for item in search["items"]
        ):
            raise RuntimeError("installed-wheel custom capture/search failed")

        legacy = data / "vault" / "notes" / "synthetic-schema-8.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(LEGACY_ENTRY, encoding="utf-8")

        before = root / "before-upgrade.zip"
        archive_data(data, before)
        preview = json.loads(run(*base, "migrate", "--dry-run").stdout)
        if len(preview["migrated"]) != 1 or preview["migrated"][0]["from"] != 8:
            raise RuntimeError(
                "release migration preview did not contain one schema-8 entry"
            )
        run(*base, "migrate", "--apply")
        run(*base, "validate")
        upgraded = json.loads(run(*base, "doctor").stdout)
        if upgraded["entry_schema_versions"] != {current: 3}:
            raise RuntimeError(
                "release upgrade did not move all three entries to schema "
                f"{current}: {upgraded['entry_schema_versions']}"
            )

        restore_data(before, data)
        run(*base, "validate")
        restored = json.loads(run(*base, "doctor").stdout)
        if restored["entry_schema_versions"] != {"8": 1, current: 2}:
            raise RuntimeError(
                "release restore did not recover the schema-8 boundary: "
                f"{restored['entry_schema_versions']}"
            )

        mcp = binary / (
            "noetrail-mcp.exe" if sys.platform == "win32" else "noetrail-mcp"
        )
        verify_mcp_retrieval(mcp, data, config, book["id"], root)
        run(*base, "index", "rebuild")
        verify_mcp_retrieval(mcp, data, config, book["id"], root)

        version = run(str(noetrail), "--version").stdout.strip()
        run(
            str(
                binary
                / ("noetrail-mcp.exe" if sys.platform == "win32" else "noetrail-mcp")
            ),
            "--help",
        )
        run(
            str(
                binary
                / (
                    "noetrail-bookmark-fetcher.exe"
                    if sys.platform == "win32"
                    else "noetrail-bookmark-fetcher"
                )
            ),
            "--help",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "version": version,
                    "installed_from": wheel.name,
                    "upgrade": f"8->{current}",
                    "restore": "schema-8 backup recovered and validated",
                    "mcp": "stdio retrieval passed in both read modes, "
                    "with/without index",
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

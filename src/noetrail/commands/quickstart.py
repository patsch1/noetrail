#!/usr/bin/env python3
"""The `quickstart` command: one instance, sample entries, an MCP line.

Trying Noetrail used to take a checkout, a virtual environment, an `init`
with two root flags, a hand-written import, and a hand-written MCP server
definition before anything was visible. That is five decisions before the
first answer. This command makes all five for the reader, prints what it did,
and prints the one block a client needs -- while staying inside the rules
every other write path follows: the exclusive vault lock, atomic writes, and
entries `validate` accepts.

It is deliberately not a demo mode. What it writes is ordinary content in the
reader's own instance, so the next command they run is a real one.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import uuid

from noetrail.commands.maintenance import initialize_instance
from noetrail.errors import InvalidRequest
from noetrail.frontmatter import pack_builtin_metadata, parse_frontmatter
from noetrail.layout import NoetrailLayout
from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
)
from noetrail.normalize import now_iso, slugify
from noetrail.schema import SchemaRegistry
from noetrail.store import atomic_write_entry, entry_directory, entry_paths, vault_lock

DEFAULT_DIRECTORY = "noetrail"

# Deterministic, so a repeated quickstart recognises its own entries instead
# of writing a second copy of them.
SAMPLE_NAMESPACE = "https://noetrail.local/quickstart/"


@dataclass(frozen=True)
class Sample:
    """One entry `quickstart` writes into the reader's new instance."""

    key: str
    entry_type: str
    title: str
    body: str
    tags: tuple[str, ...] = ()
    relations: tuple[tuple[str, str], ...] = ()
    unresolved: tuple[tuple[str, str], ...] = field(default=())


SAMPLES: tuple[Sample, ...] = (
    Sample(
        key="welcome",
        entry_type="note",
        title="What Noetrail stores",
        tags=("noetrail", "quickstart"),
        body=(
            "Every entry is one Markdown file with a JSON-typed frontmatter "
            "envelope: a stable ID, a type, timestamps, tags, and typed "
            "relations. Reading this vault needs nothing but an editor.\n\n"
            "This entry came from `noetrail quickstart`. Trash it once your "
            "own notes are in here."
        ),
    ),
    Sample(
        key="review",
        entry_type="thought",
        title="Anything captured lands in the review inbox",
        tags=("noetrail", "quickstart"),
        body=(
            "A capture stays `unreviewed` until a human confirms it, which "
            "is what keeps an agent from quietly filling a vault. Run "
            "`noetrail review` to see the queue."
        ),
    ),
    Sample(
        key="relations",
        entry_type="note",
        title="Relations are typed and unresolved ones stay visible",
        tags=("noetrail", "quickstart", "relations"),
        relations=(("related_to", "welcome"),),
        unresolved=(("related_to", "a note you have not written yet"),),
        body=(
            "A relation points at a stable entry ID through a configured "
            "predicate. A reference that does not resolve yet is kept as an "
            "unresolved relation instead of being dropped, which is how an "
            "import can be honest about what it could not connect.\n\n"
            "`noetrail relations <id>` shows both directions."
        ),
    ),
)


def sample_entry_id(key: str) -> str:
    return "kn_" + uuid.uuid5(uuid.NAMESPACE_URL, SAMPLE_NAMESPACE + key).hex


def existing_entry_ids(root: Path) -> set[str]:
    found: set[str] = set()
    for path in entry_paths(root):
        try:
            metadata, _ = parse_frontmatter(path)
        except ValueError:
            continue
        entry_id = metadata.get("id")
        if isinstance(entry_id, str):
            found.add(entry_id)
    return found


def write_samples(root: Path, registry: SchemaRegistry) -> list[dict[str, str]]:
    """Write the sample entries that are not in the vault yet."""

    timestamp = now_iso()
    present = existing_entry_ids(root)
    created: list[dict[str, str]] = []
    for sample in SAMPLES:
        entry_id = sample_entry_id(sample.key)
        if entry_id in present:
            continue
        metadata: dict[str, object] = {
            "id": entry_id,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "type": sample.entry_type,
            "title": sample.title,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": "unreviewed",
            "sensitivity": "normal",
            "tags": sorted(sample.tags),
            "relations": [
                {"predicate": predicate, "target": sample_entry_id(target)}
                for predicate, target in sample.relations
            ],
            "origin": "import",
        }
        if sample.unresolved:
            metadata["unresolved_relations"] = [
                {"predicate": predicate, "reference": reference}
                for predicate, reference in sample.unresolved
            ]
        metadata = pack_builtin_metadata(metadata, registry)
        filename = f"quickstart--{slugify(sample.title)}--{entry_id[3:11]}.md"
        target = entry_directory(root, sample.entry_type) / filename
        atomic_write_entry(target, metadata, sample.body, must_not_exist=True)
        created.append(
            {
                "id": entry_id,
                "type": sample.entry_type,
                "title": sample.title,
                "path": str(target.relative_to(root)),
            }
        )
    return created


def mcp_configuration(layout: NoetrailLayout) -> dict[str, object]:
    """The server definition an MCP client expects, with absolute roots."""

    return {
        "mcpServers": {
            "noetrail": {
                "command": "noetrail-mcp",
                "args": [
                    "--data-root",
                    str(layout.data_root),
                    "--config-root",
                    str(layout.config_root),
                ],
            }
        }
    }


def command_quickstart(args: argparse.Namespace) -> int:
    """Create an instance, fill it with samples, print how to reach it."""

    if args.root is not None:
        raise InvalidRequest(
            "quickstart creates a separated instance; use --data-root and "
            "--config-root instead of --root"
        )
    base = (
        Path(args.path).expanduser()
        if args.path
        else Path.home() / DEFAULT_DIRECTORY
    )
    layout, registry, created_directories = initialize_instance(
        args.data_root or base / "data",
        args.config_root or base / "config",
        args.builtins_root,
    )
    root = layout.data_root
    with vault_lock(root, exclusive=True):
        created = write_samples(root, registry)

    configuration = mcp_configuration(layout)
    roots = [
        "--data-root",
        str(layout.data_root),
        "--config-root",
        str(layout.config_root),
    ]
    if args.json:
        print(
            json.dumps(
                {
                    "data_root": str(layout.data_root),
                    "config_root": str(layout.config_root),
                    "created_directories": [
                        str(path) for path in created_directories
                    ],
                    "created_entries": created,
                    "root_arguments": roots,
                    "mcp_config": configuration,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    joined = " ".join(roots)
    print("Noetrail is ready.\n")
    print(f"  data root     {layout.data_root}")
    print(f"  config root   {layout.config_root}")
    print(f"  new entries   {len(created)}\n")
    print("Look around:\n")
    print(f"  noetrail {joined} search relations")
    print(f"  noetrail {joined} review")
    print(f"  noetrail {joined} inventory\n")
    print("Connect an agent - add this to your MCP client's configuration:\n")
    print(json.dumps(configuration, ensure_ascii=False, indent=2))
    print("\nNext: docs/quickstart.md and docs/integrations/mcp-clients.md")
    return 0

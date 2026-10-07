#!/usr/bin/env python3
"""Installation-level commands: migrate, init, doctor, validate, schema."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from noetrail.attachments import validate_attachment_blob
from noetrail.constants import ATTACHMENT_PATH_PATTERN, COMPATIBLE_SCHEMA_VERSIONS
from noetrail.errors import (
    ConfigurationError,
    InvalidRequest,
)
from noetrail.frontmatter import (
    builtin_type_layouts,
    parse_frontmatter,
    runtime_metadata,
)
from noetrail.index import (
    drop_index,
    index_path,
)
from noetrail.index import refresh as refresh_index
from noetrail.index import status as index_status
from noetrail.layout import (
    LayoutError,
    NoetrailLayout,
    default_application_root,
    default_builtins_root,
    default_config_root,
    environment_builtins_root,
    environment_config_root,
    environment_data_root,
    resolve_layout,
)
from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
    get_schema_version,
    migrate_entry,
)
from noetrail.relations import load_relation_types
from noetrail.schema import (
    COMPONENT_PATTERN,
    SchemaPackError,
    SchemaRegistry,
    load_pack_candidate,
)
from noetrail.store import atomic_write_entry, managed_paths
from noetrail.temporal import is_temporal, supersession_errors
from noetrail.validation import validate_metadata


def command_migrate(args: argparse.Namespace, root: Path) -> int:
    # `migrate` used to write by default and preview only under `--dry-run`,
    # which made it the one destructive command that did not match `import` and
    # `purge`. Rewriting every entry in the vault is now opt-in.
    if args.dry_run and not args.apply:
        print(
            "note: --dry-run is the default and no longer needed; "
            "pass --apply to write.",
            file=sys.stderr,
        )
    dry_run = not args.apply
    plans: list[
        tuple[
            Path,
            dict[str, object],
            str,
            int,
            list[str],
            dict[str, object],
        ]
    ] = []
    errors: list[str] = []
    unchanged = 0

    for path in managed_paths(root):
        relative = path.relative_to(root)
        try:
            metadata, body = parse_frontmatter(path)
            migrated_metadata, migrated_body, start_version, steps = migrate_entry(
                metadata,
                body,
                builtin_types=builtin_type_layouts(args.registry),
            )
        except (ValueError, OSError) as exc:
            errors.append(f"{relative}: {exc}")
            continue
        if steps:
            migrated_attributes = migrated_metadata.get("attributes")
            moved_fields = (
                sorted(
                    field
                    for field, value in migrated_attributes.items()
                    if field in metadata and metadata.get(field) == value
                )
                if isinstance(migrated_attributes, dict)
                else []
            )
            changes = {
                "moved_to_attributes": moved_fields,
                "added_top_level": sorted(
                    set(migrated_metadata) - set(metadata)
                ),
                "removed_top_level": sorted(
                    set(metadata) - set(migrated_metadata) - set(moved_fields)
                ),
                "body_changed": migrated_body != body,
            }
            plans.append(
                (
                    path,
                    migrated_metadata,
                    migrated_body,
                    start_version,
                    steps,
                    changes,
                )
            )
        else:
            unchanged += 1

    # Validate the *result* before writing anything. `migrate_8_to_9` moves
    # fields; it cannot know that a field became required in the meantime. An
    # entry that failed this check used to be written anyway, reported as a
    # success, and was then rejected by every subsequent write command --
    # unrecoverable without hand-editing the Markdown.
    predicates = load_relation_types(args.layout)
    for path, migrated_metadata, _, _, _, _ in plans:
        relative = path.relative_to(root)
        errors.extend(
            f"{relative}: after migration, {message.split(': ', 1)[-1]}"
            for message in validate_metadata(
                relative,
                runtime_metadata(migrated_metadata),
                predicates,
                args.registry,
                in_trash=relative.parts[0] == "trash",
            )
        )

    if errors:
        for error in errors:
            print(f"ERROR {error}")
        print("Migration preflight failed; no files were changed.")
        return 1

    migrated_results = [
        {
            "path": str(path.relative_to(root)),
            "from": start_version,
            "to": CURRENT_SCHEMA_VERSION,
            "steps": steps,
            "changes": changes,
        }
        for path, _, _, start_version, steps, changes in plans
    ]
    backups: list[str] = []
    if not dry_run:
        backup_root: Path | None = None
        if args.backup_dir is not None:
            backup_root = Path(args.backup_dir).expanduser()
            if backup_root.exists() and any(backup_root.iterdir()):
                raise InvalidRequest(
                    "Backup directory must be empty: " f"{backup_root}"
                )
            for path, _, _, _, _, _ in plans:
                relative = path.relative_to(root)
                destination = backup_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                backups.append(str(relative))
        for path, metadata, body, _, _, _ in plans:
            atomic_write_entry(path, metadata, body)

    print(
        json.dumps(
            {
                "dry_run": dry_run,
                "current_schema_version": CURRENT_SCHEMA_VERSION,
                "migrated": migrated_results,
                "unchanged": unchanged,
                "backup_dir": args.backup_dir,
                "backed_up": sorted(backups),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def initialize_instance(
    data_value: str | Path,
    config_value: str | Path | None = None,
    builtins_value: str | Path | None = None,
) -> tuple[NoetrailLayout, SchemaRegistry, list[Path]]:
    """Create the empty private runtime directories and validate the result.

    Split out of ``command_init`` so ``quickstart`` can create the same
    instance without shelling out to the CLI or reprinting init's report:
    there must be exactly one place that decides which directories an
    instance consists of and what makes it valid.
    """

    config_value = (
        config_value or environment_config_root() or default_config_root()
    )
    application = default_application_root()
    builtins_value = (
        builtins_value
        or environment_builtins_root()
        or default_builtins_root(application)
    )

    data = Path(data_value).expanduser().resolve()
    config = Path(config_value).expanduser().resolve()
    try:
        builtins = Path(builtins_value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError(
            f"Built-in resources are unavailable: {builtins_value}"
        ) from exc
    if len({data, config, builtins}) != 3:
        raise InvalidRequest(
            "Separated data, config, and built-ins roots must be distinct"
        )
    if not (builtins / "SPEC.md").is_file():
        raise ConfigurationError("Built-in resources do not contain SPEC.md")

    requested = [
        data,
        data / "vault",
        data / "vault" / ".locks",
        data / "trash",
        data / "imports",
        data / "imports" / "raw",
        data / "imports" / "work",
        config,
        config / "packs",
    ]
    created: list[Path] = []
    for directory in requested:
        if directory.is_symlink():
            raise ConfigurationError(
                f"Initialization target is a symbolic link: {directory}"
            )
        if directory.exists() and not directory.is_dir():
            raise ConfigurationError(
                f"Initialization target is not a directory: {directory}"
            )
        if not directory.exists():
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            created.append(directory)

    try:
        layout = resolve_layout(
            data_root=data,
            config_root=config,
            builtins_root=builtins,
            application_root=application,
        )
        registry = SchemaRegistry.load(layout)
    except (LayoutError, SchemaPackError) as exc:
        raise ConfigurationError(f"Initialized roots failed validation: {exc}") from exc
    return layout, registry, created


def command_init(args: argparse.Namespace) -> int:
    """Create an empty separated instance without copying application files."""

    if args.root is not None:
        raise InvalidRequest(
            "init creates a separated installation; use --data-root instead "
            "of --root"
        )
    data_value = args.data_root or environment_data_root()
    if data_value is None:
        raise InvalidRequest(
            "init requires --data-root or NOETRAIL_DATA_ROOT "
            "(legacy: KNOWLEDGE_DATA_ROOT)"
        )
    layout, registry, created = initialize_instance(
        data_value, args.config_root, args.builtins_root
    )
    print(
        json.dumps(
            {
                "initialized": True,
                "idempotent": not created,
                "data_root": str(layout.data_root),
                "config_root": str(layout.config_root),
                "builtins_root": str(layout.builtins_root),
                "created_directories": [str(path) for path in created],
                "pack_count": len(registry.packs),
                "type_count": len(registry.types),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_doctor(args: argparse.Namespace, root: Path) -> int:
    """Report installation health without exposing entry titles or bodies."""

    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    layout: NoetrailLayout = args.layout
    for name, path, wanted in (
        ("data_root", layout.data_root, os.R_OK | os.W_OK | os.X_OK),
        ("config_root", layout.config_root, os.R_OK | os.X_OK),
        ("builtins_root", layout.builtins_root, os.R_OK | os.X_OK),
    ):
        status = "ok" if os.access(path, wanted) else "error"
        add(name, status, "accessible" if status == "ok" else "permission denied")

    for relative in ("vault", "trash", "imports/raw", "imports/work"):
        path = root / relative
        if path.is_symlink():
            add(f"data_directory:{relative}", "error", "symbolic link")
        elif not path.exists():
            add(f"data_directory:{relative}", "warning", "missing")
        elif not path.is_dir():
            add(f"data_directory:{relative}", "error", "not a directory")
        else:
            add(f"data_directory:{relative}", "ok", "present")

    registry: SchemaRegistry = args.registry
    add(
        "schema_registry",
        "ok",
        f"{len(registry.packs)} packs and {len(registry.types)} types",
    )

    version_counts: dict[int, int] = {}
    unreadable = 0
    temporal_entries = 0
    contradictions: list[str] = []
    for path in managed_paths(root):
        try:
            metadata, _ = parse_frontmatter(path)
            version = get_schema_version(metadata)
        except (OSError, ValueError):
            unreadable += 1
            continue
        version_counts[version] = version_counts.get(version, 0) + 1
        relations = metadata.get("relations")
        if isinstance(relations, list) and any(
            is_temporal(relation) for relation in relations
        ):
            temporal_entries += 1
        contradictions.extend(
            f"{path.relative_to(root)}: {message}"
            for message in supersession_errors(relations)
        )
    if unreadable:
        add(
            "entry_frontmatter",
            "error",
            f"{unreadable} entries could not be parsed",
        )
    else:
        add("entry_frontmatter", "ok", "all entries parsed")

    # A contradiction is not a damaged file: both statements parse, validate
    # structurally, and can be read. What is broken is the claim they make
    # together, so `doctor` reports it as an error and points at `validate`,
    # which names the individual relation.
    if contradictions:
        add(
            "relation_validity",
            "error",
            f"{len(contradictions)} contradictory supersession record(s); "
            "run `noetrail validate`",
        )
    else:
        add(
            "relation_validity",
            "ok",
            f"{temporal_entries} entries carry relation validity intervals",
        )

    unsupported = sum(
        count
        for version, count in version_counts.items()
        if version not in COMPATIBLE_SCHEMA_VERSIONS
    )
    compatible_legacy = sum(
        count
        for version, count in version_counts.items()
        if version in COMPATIBLE_SCHEMA_VERSIONS
        and version != CURRENT_SCHEMA_VERSION
    )
    if unsupported:
        add(
            "schema_versions",
            "error",
            f"{unsupported} entries use unsupported schema versions",
        )
    elif compatible_legacy:
        add(
            "schema_versions",
            "warning",
            f"{compatible_legacy} readable entries below schema "
            f"{CURRENT_SCHEMA_VERSION} have a migration available",
        )
    else:
        add(
            "schema_versions",
            "ok",
            f"all entries use schema {CURRENT_SCHEMA_VERSION}",
        )

    # The index is a cache, so no state of it is an error: a stale or damaged
    # index costs a slower search, never a wrong answer. Reporting it as a
    # warning is what keeps `doctor` honest about the difference.
    index_report = index_status(root, layout, registry)
    index_state = str(index_report["state"])
    if index_state == "absent":
        add("search_index", "ok", "no index built; search scans the vault")
    elif index_state == "fresh":
        add(
            "search_index",
            "ok",
            f"{index_report.get('document_count')} entries indexed, "
            f"{index_report.get('bytes')} bytes",
        )
    else:
        add(
            "search_index",
            "warning",
            f"{index_state}: {index_report.get('detail', 'unusable')}; "
            "run `noetrail index rebuild`",
        )

    ok = not any(check["status"] == "error" for check in checks)
    print(
        json.dumps(
            {
                "ok": ok,
                "current_schema_version": CURRENT_SCHEMA_VERSION,
                "search_index": index_report,
                "entry_schema_versions": {
                    str(version): count
                    for version, count in sorted(version_counts.items())
                },
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


def command_index_status(args: argparse.Namespace, root: Path) -> int:
    """Report whether the derived BM25 index matches the vault.

    Deliberately a handler of its own rather than a branch of a shared `index`
    handler. `tests/test_locking.py` classifies a handler as writing when its
    call graph reaches a write, and a single function covering `status`,
    `rebuild`, and `drop` would have to take the exclusive lock for all three
    -- turning a diagnostic read into something that blocks every writer.
    """

    report = index_status(root, args.layout, args.registry, verify=args.verify)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # A cache that does not match the vault is not a healthy installation, but
    # it is also not lost data: search still answers correctly, from the files.
    return 0 if report["state"] in {"fresh", "absent"} else 1


def command_index_rebuild(args: argparse.Namespace, root: Path) -> int:
    """Create or refresh the index under the exclusive vault lock.

    Exclusive, although nothing in the vault changes: a rebuild that ran
    beside a capture would record a mixture of before and after that never
    existed as a whole, and the signature digest it stored would then describe
    a vault state no reader can reproduce.
    """

    if args.full:
        drop_index(args.layout)
    rebuilt = refresh_index(root, args.layout, args.registry, force=args.full)
    print(
        json.dumps(
            {
                "path": str(index_path(args.layout)),
                "mode": "full" if args.full else rebuilt.reason,
                "documents": rebuilt.document_count,
                "vocabulary_size": rebuilt.vocabulary_size,
                "added": rebuilt.added,
                "updated": rebuilt.updated,
                "removed": rebuilt.removed,
                "bytes": rebuilt.bytes_written,
                "seconds": round(rebuilt.seconds, 3),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_index_drop(args: argparse.Namespace, root: Path) -> int:
    """Delete the index. Search keeps working by scanning the vault."""

    removed = drop_index(args.layout)
    print(
        json.dumps(
            {
                "dropped": removed,
                "path": str(index_path(args.layout)),
                "detail": (
                    "search falls back to scanning the vault"
                    if removed
                    else "no index was present"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_validate(args: argparse.Namespace, root: Path) -> int:
    predicates = load_relation_types(args.layout)
    errors: list[str] = []
    parsed: list[tuple[Path, dict[str, object], bool]] = []

    for path in managed_paths(root):
        relative = path.relative_to(root)
        in_trash = relative.parts[0] == "trash"
        try:
            metadata, _ = parse_frontmatter(path)
        except ValueError as exc:
            errors.append(f"{relative}: {exc}")
            continue
        parsed.append((relative, metadata, in_trash))
        errors.extend(
            validate_metadata(
                relative,
                metadata,
                predicates,
                args.registry,
                in_trash=in_trash,
            )
        )

    warnings: list[str] = []
    ids: dict[str, Path] = {}
    active_ids: set[str] = set()
    trashed_ids: set[str] = set()
    canonical_urls: dict[str, Path] = {}
    attachment_ids: dict[str, Path] = {}
    referenced_attachment_paths: set[str] = set()
    validated_attachment_paths: set[str] = set()
    for path, metadata, in_trash in parsed:
        entry_id = metadata.get("id")
        if not isinstance(entry_id, str):
            pass
        elif entry_id in ids:
            errors.append(f"{path}: duplicate id also used by {ids[entry_id]}")
        else:
            ids[entry_id] = path
            (trashed_ids if in_trash else active_ids).add(entry_id)
        canonical_url = metadata.get("canonical_url")
        if (
            not in_trash
            and metadata.get("type") == "bookmark"
            and isinstance(canonical_url, str)
        ):
            if canonical_url in canonical_urls:
                errors.append(
                    f"{path}: duplicate canonical_url also used by "
                    f"{canonical_urls[canonical_url]}"
                )
            else:
                canonical_urls[canonical_url] = path

        attachments = metadata.get("attachments", [])
        if not isinstance(attachments, list):
            continue
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            attachment_id = attachment.get("id")
            if isinstance(attachment_id, str):
                if attachment_id in attachment_ids:
                    errors.append(
                        f"{path}: attachment {index} id is also used by "
                        f"{attachment_ids[attachment_id]}"
                    )
                else:
                    attachment_ids[attachment_id] = path
            attachment_path = attachment.get("path")
            if not isinstance(
                attachment_path, str
            ) or not ATTACHMENT_PATH_PATTERN.fullmatch(attachment_path):
                continue
            referenced_attachment_paths.add(attachment_path)
            if attachment_path in validated_attachment_paths:
                continue
            validated_attachment_paths.add(attachment_path)
            for error in validate_attachment_blob(root, attachment):
                errors.append(f"{path}: attachment {index} {error}")

    for path, metadata, in_trash in parsed:
        relations = metadata.get("relations", [])
        if not isinstance(relations, list):
            continue
        for index, relation in enumerate(relations):
            if isinstance(relation, dict):
                target = relation.get("target")
                if isinstance(target, str) and target not in ids:
                    errors.append(
                        f"{path}: relation {index} has missing target {target}"
                    )
                elif (
                    not in_trash
                    and isinstance(target, str)
                    and target in trashed_ids
                ):
                    warnings.append(
                        f"{path}: relation {index} targets trashed entry {target}"
                    )

    attachments_root = root / "vault" / "attachments"
    if attachments_root.exists():
        for stored in sorted(attachments_root.rglob("*")):
            stored_relative = stored.relative_to(root).as_posix()
            if stored.is_symlink():
                errors.append(f"{stored_relative}: symbolic links are not allowed")
            elif (
                stored.is_file()
                and stored_relative not in referenced_attachment_paths
            ):
                warnings.append(f"{stored_relative}: unreferenced attachment blob")

    if errors:
        for error in errors:
            print(f"ERROR {error}")
        print(f"Validation failed with {len(errors)} error(s).")
        return 1
    for warning in warnings:
        print(f"WARN {warning}")
    print(
        f"Validation passed for {len(active_ids)} active and "
        f"{len(trashed_ids)} trashed entries with {len(warnings)} warning(s)."
    )
    return 0


def command_schema(args: argparse.Namespace, root: Path) -> int:
    del root
    registry: SchemaRegistry = args.registry
    if args.schema_command in {"list", "validate"}:
        print(
            json.dumps(
                registry.summary(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.schema_command == "explain":
        try:
            type_definition = registry.require_type(args.type_id)
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc
        print(
            json.dumps(
                type_definition.as_dict(include_schema=True),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.schema_command == "validate-pack":
        try:
            pack = load_pack_candidate(args.path)
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc
        print(
            json.dumps(
                {
                    "valid": True,
                    "pack": pack.as_dict(),
                    "types": [
                        definition.as_dict(include_schema=True)
                        for definition in pack.types.values()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    raise InvalidRequest(f"Unsupported schema command: {args.schema_command}")


def command_schema_init(args: argparse.Namespace, root: Path) -> int:
    """Create one validated, inert pack without mixing writes into inspection."""

    del root
    if COMPONENT_PATTERN.fullmatch(args.pack_id) is None:
        raise InvalidRequest(
            "pack ID must be lowercase letters, digits, or hyphens"
        )
    if COMPONENT_PATTERN.fullmatch(args.type_id) is None:
        raise InvalidRequest(
            "type ID must be lowercase letters, digits, or hyphens"
        )
    if args.output is None:
        args.layout.config_root.mkdir(parents=True, exist_ok=True)
        parent = args.layout.config_root / "packs"
        parent.mkdir(exist_ok=True)
    else:
        raw_parent = Path(args.output).expanduser()
        if raw_parent.is_symlink():
            raise InvalidRequest("schema pack output must not be a symbolic link")
        try:
            parent = raw_parent.resolve(strict=True)
        except OSError as exc:
            raise InvalidRequest(
                "schema pack output directory is unavailable"
            ) from exc
        if not parent.is_dir():
            raise InvalidRequest("schema pack output must be a directory")
    destination = parent / args.pack_id
    if destination.exists() or destination.is_symlink():
        raise InvalidRequest(
            f"schema pack destination already exists: {destination}"
        )

    pack_title = args.pack_id.replace("-", " ").title()
    type_title = args.type_id.replace("-", " ").title()
    manifest = (
        "format_version: 1\n"
        f"id: {json.dumps(args.pack_id)}\n"
        "version: 1\n"
        f"title: {json.dumps(pack_title)}\n"
        f"description: {json.dumps(f'Declarative schema pack for {pack_title}.')}\n"
        "types:\n"
        f"  {args.type_id}:\n"
        f"    title: {json.dumps(type_title)}\n"
        f"    description: {json.dumps(f'A {type_title.lower()} entry.')}\n"
        '    body_sections: ["Notes"]\n'
    )
    with tempfile.TemporaryDirectory(
        prefix=".noetrail-pack-",
        dir=parent,
    ) as temporary:
        staged = Path(temporary) / args.pack_id
        staged.mkdir()
        (staged / "pack.yaml").write_text(manifest, encoding="utf-8")
        try:
            pack = load_pack_candidate(staged)
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc
        staged.replace(destination)
    print(
        json.dumps(
            {
                "created": str(destination),
                "pack": pack.as_dict(),
                "next": f"noetrail schema validate-pack {destination}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0

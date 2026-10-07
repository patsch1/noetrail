#!/usr/bin/env python3
"""The `import obsidian` and `import basic-memory` commands.

Both read a foreign Markdown vault that the user copied below
``<data-root>/imports/raw/``. They share one engine because the two formats
overlap almost completely -- YAML frontmatter, ``[[wikilinks]]``, ``#tags`` --
and differ only in which properties carry meaning and how relations are
spelled. The format knowledge itself lives in ``noetrail.obsidian`` and
``noetrail.basic_memory``, each with the documentation its rules came from.

The safety posture is the one ``import markdown`` established, plus what a
whole foreign vault adds:

- the source must resolve below ``imports/raw`` (``markdown_import_source``);
- a symbolic link anywhere in the tree is skipped and reported, never
  followed, so a link into ``/etc`` cannot be read through a note;
- a wikilink target is matched against the in-memory listing of the scanned
  tree and never used to open a path, so ``[[../../etc/passwd]]`` resolves to
  nothing rather than to a file;
- every file is opened with ``O_NOFOLLOW`` under a size ceiling;
- an embedded image passes the same magic-byte check as an inbox attachment;
- ``--apply`` is opt-in, and any ``BaseException`` during the write phase
  rolls back every entry and every blob this run created.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from noetrail.attachments import (
    attachment_blob_path,
    detect_attachment_image,
    normalize_attachment_name,
    write_attachment_blob,
)
from noetrail.basic_memory import observation_tags, parse_sections
from noetrail.commands.importer import markdown_import_source, read_markdown_import
from noetrail.constants import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_ENTRY,
    MAX_MARKDOWN_IMPORT_FILES,
    MAX_VAULT_IMPORT_FILES,
    MAX_VAULT_IMPORT_RELATIONS,
    MAX_VAULT_IMPORT_REPORT_ITEMS,
    MAX_VAULT_IMPORT_UNRESOLVED,
    OBSIDIAN_CONFIG_DIRECTORY,
    OBSIDIAN_PRESENTATION_PROPERTIES,
)
from noetrail.errors import (
    InvalidRequest,
    StorageError,
)
from noetrail.frontmatter import pack_builtin_metadata, parse_frontmatter
from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
)
from noetrail.normalize import (
    normalize_alias_list,
    normalize_tag_list,
    now_iso,
    slugify,
)
from noetrail.obsidian import (
    IMAGE_SUFFIXES,
    LossyConstructs,
    ObsidianFrontmatterError,
    first_heading,
    inline_tags,
    lossy_constructs,
    normalize_tag,
    parse_properties,
    split_frontmatter,
    wikilinks,
)
from noetrail.provenance import with_provenance
from noetrail.relations import load_relation_types
from noetrail.store import atomic_write_entry, entry_directory, managed_paths

# Obsidian's `[[Note]]` is untyped, so the symmetric predicate is the only
# honest mapping. Basic Memory writes its own type in front of the link and
# keeps it whenever the instance configures that predicate.
DEFAULT_PREDICATE = "related_to"

MARKDOWN_SUFFIX = ".md"

# Properties this importer reads into the entry envelope rather than
# preserving verbatim under `source.frontmatter`.
ENVELOPE_PROPERTIES = {"title", "tags", "aliases"}


@dataclass
class VaultScan:
    """What one walk of the foreign vault found, before anything is read."""

    markdown: list[Path] = field(default_factory=list)
    assets: dict[str, Path] = field(default_factory=dict)
    canvas: list[str] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    config_present: bool = False
    ignored_hidden: list[str] = field(default_factory=list)
    total_files: int = 0


@dataclass
class SourceNote:
    """One parsed foreign note, before it is turned into an entry."""

    path: Path
    relative: str
    vault_relative: str
    digest: str
    title: str
    body: str
    properties: dict[str, object]
    tags: list[str]
    aliases: list[str]
    # (target, is_embed, source predicate or None for an untyped wikilink)
    links: list[tuple[str, bool, str | None]]
    link_names: list[str]
    lossy: LossyConstructs
    observation_categories: list[str]
    entry_id: str = ""
    original_id: str = ""


@dataclass
class EntryPlan:
    """One entry this run would create, with everything it needs to write."""

    source: str
    entry_id: str
    title: str
    target: Path
    metadata: dict[str, object]
    body: str
    blobs: list[tuple[Path, str, str]]
    relation_count: int
    unresolved_count: int
    attachment_count: int


def _posix(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _bounded(items: list[str]) -> list[str]:
    return sorted(set(items))[:MAX_VAULT_IMPORT_REPORT_ITEMS]


def scan_vault(source: Path) -> VaultScan:
    """List the foreign vault without following or reading anything.

    A symbolic link is recorded and skipped rather than aborting the run: a
    personal vault legitimately contains them, and one link must not block a
    whole migration. Nothing below a link is ever opened, so skipping is the
    safe outcome rather than a relaxation of the rule ``import markdown``
    applies to curated exports.
    """

    scan = VaultScan()
    for path in sorted(source.rglob("*")):
        parts = path.relative_to(source).parts
        relative = "/".join(parts)
        hidden = next((part for part in parts if part.startswith(".")), None)
        if hidden is not None:
            if hidden == OBSIDIAN_CONFIG_DIRECTORY:
                scan.config_present = True
            elif parts[-1] == hidden:
                scan.ignored_hidden.append(relative)
            continue
        if path.is_symlink():
            scan.symlinks.append(relative)
            continue
        if not path.is_file():
            continue
        scan.total_files += 1
        if scan.total_files > MAX_VAULT_IMPORT_FILES:
            raise InvalidRequest(
                f"Vault import contains more than {MAX_VAULT_IMPORT_FILES} files"
            )
        suffix = path.suffix.casefold()
        if suffix == MARKDOWN_SUFFIX:
            scan.markdown.append(path)
            continue
        scan.assets[relative.casefold()] = path
        if suffix == ".canvas":
            scan.canvas.append(relative)
        elif suffix not in IMAGE_SUFFIXES:
            scan.other_files.append(relative)
    if len(scan.markdown) > MAX_MARKDOWN_IMPORT_FILES:
        raise InvalidRequest(
            f"Vault import contains more than {MAX_MARKDOWN_IMPORT_FILES} "
            "Markdown files"
        )
    return scan


def _property_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [
            str(item)
            for item in value
            if isinstance(item, (str, int, float)) and not isinstance(item, bool)
        ]
    if isinstance(value, str):
        # Obsidian documents `tags` as a YAML list, but a comma-separated
        # scalar is what many older vaults and templates actually contain.
        return [part for part in (item.strip() for item in value.split(",")) if part]
    return []


def _storable(value: object) -> object | None:
    """Reduce a parsed property to a value the entry frontmatter can hold.

    ``None`` means "this shape is not preserved"; the caller reports it. The
    entry envelope is JSON on one line per key, so a nested structure is
    refused rather than flattened into something the source did not say.
    """

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list):
        reduced: list[object] = []
        for item in value[:256]:
            if item is None or isinstance(item, (bool, int, float)):
                reduced.append(item)
            elif isinstance(item, str):
                reduced.append(item[:1000])
            else:
                return None
        return reduced
    return None


def _note_title(properties: dict[str, object], body: str, path: Path) -> str:
    declared = properties.get("title")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()[:500]
    heading = first_heading(body)
    if heading:
        return heading
    return (path.stem.replace("_", " ").strip() or "Imported note")[:500]


def read_vault_image(path: Path) -> tuple[bytes, str, str] | str:
    """Return image bytes with their detected type, or a refusal sentence."""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "file could not be opened"
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            return "source is not a regular file"
        if details.st_size < 1:
            return "file is empty"
        if details.st_size > MAX_ATTACHMENT_BYTES:
            return f"file exceeds the {MAX_ATTACHMENT_BYTES}-byte limit"
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(MAX_ATTACHMENT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > MAX_ATTACHMENT_BYTES:
        return f"file exceeds the {MAX_ATTACHMENT_BYTES}-byte limit"
    detected = detect_attachment_image(data)
    if detected is None:
        return "file is not a supported image"
    return data, detected[0], detected[1]


def safe_link_target(target: str) -> str | None:
    """Normalise link text, or refuse it as a path expression.

    The result is only ever used as a dictionary key, so refusing here is not
    what prevents traversal -- not touching the filesystem is. It is what
    keeps a climbing target from being silently normalised into a name that
    happens to match a real note.
    """

    candidate = target.strip().replace("\\", "/")
    if not candidate or "\x00" in candidate:
        return None
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


class LinkIndex:
    """Resolve a wikilink against the scanned tree, never against the disk."""

    def __init__(self) -> None:
        self._by_path: dict[str, str] = {}
        self._by_name: dict[str, str | None] = {}

    def add(self, vault_relative: str, entry_id: str, names: list[str]) -> None:
        stem = vault_relative[: -len(MARKDOWN_SUFFIX)]
        self._by_path[vault_relative.casefold()] = entry_id
        self._by_path[stem.casefold()] = entry_id
        for name in [Path(stem).name, *names]:
            key = name.casefold().strip()
            if not key:
                continue
            # A name used by more than one note stays ambiguous instead of
            # resolving to whichever file the walk happened to see first.
            if key in self._by_name and self._by_name[key] != entry_id:
                self._by_name[key] = None
            else:
                self._by_name.setdefault(key, entry_id)

    def resolve(self, target: str) -> str | None:
        candidate = safe_link_target(target)
        if candidate is None:
            return None
        key = candidate.casefold()
        found = self._by_path.get(key)
        if found is not None:
            return found
        if key.endswith(MARKDOWN_SUFFIX):
            found = self._by_path.get(key[: -len(MARKDOWN_SUFFIX)])
            if found is not None:
                return found
        return self._by_name.get(Path(key).name)


def resolve_asset(scan: VaultScan, target: str) -> Path | None:
    candidate = safe_link_target(target)
    if candidate is None:
        return None
    found = scan.assets.get(candidate.casefold())
    if found is not None:
        return found
    # Obsidian resolves a bare filename against the whole vault, which is how
    # a note in a subfolder embeds an image from the attachments folder.
    name = Path(candidate).name.casefold()
    matches = [path for key, path in scan.assets.items() if Path(key).name == name]
    return matches[0] if len(matches) == 1 else None


def parse_note(
    path: Path,
    relative: str,
    vault_relative: str,
    dialect: str,
) -> SourceNote | dict[str, str]:
    """Parse one foreign note, or describe why it cannot be imported."""

    try:
        text, digest = read_markdown_import(path)
    except ValueError as exc:
        return {"source": vault_relative, "reason": str(exc)}
    block, body = split_frontmatter(text)
    properties: dict[str, object] = {}
    if block is not None:
        try:
            properties = parse_properties(block)
        except ObsidianFrontmatterError as exc:
            return {
                "source": vault_relative,
                "reason": f"unsupported frontmatter: {exc}",
            }
    tags = [
        tag
        for tag in (
            normalize_tag(item) for item in _property_list(properties.get("tags"))
        )
        if tag is not None
    ]
    tags.extend(inline_tags(body))
    aliases = [
        alias.strip()[:200]
        for alias in _property_list(properties.get("aliases"))
        if alias.strip()
    ]
    title = _note_title(properties, body, path)
    link_names = [*aliases]
    typed_links: list[tuple[str, bool, str | None]] = []
    typed_targets: set[str] = set()
    categories: list[str] = []
    if dialect == "basic-memory":
        sections = parse_sections(body)
        tags.extend(observation_tags(sections.observations))
        # Basic Memory documents `title` as "the note's name for linking",
        # and `permalink` as its stable identifier, so both are link names
        # there. Obsidian resolves by filename, which every dialect indexes.
        link_names.append(title)
        permalink = properties.get("permalink")
        if isinstance(permalink, str) and permalink.strip():
            link_names.append(permalink.strip())
        typed_links = [
            (relation.target, False, relation.relation_type)
            for relation in sections.relations
        ]
        typed_targets = {relation.target for relation in sections.relations}
        categories = sorted(
            {observation.category for observation in sections.observations}
        )
    # A relation line's `[[Target]]` is found by the wikilink scan too; the
    # typed entry replaces it so one line does not become two relations and a
    # second predicate for the same target is not lost to a dict key.
    links: list[tuple[str, bool, str | None]] = [
        (link.target, link.embed, None)
        for link in wikilinks(body)
        if link.target and link.target not in typed_targets
    ]
    links.extend(typed_links)
    return SourceNote(
        path=path,
        relative=relative,
        vault_relative=vault_relative,
        digest=digest,
        title=title,
        body=body,
        properties=properties,
        tags=tags,
        aliases=aliases,
        links=links,
        link_names=link_names,
        lossy=lossy_constructs(text),
        observation_categories=categories,
    )


def identify(note: SourceNote, dialect: str) -> None:
    """Give the note its stable source ID and its derived entry ID.

    Basic Memory documents ``permalink`` as the note's stable identifier, so
    it is preferred over the path there. Obsidian has no such field, so the
    vault-relative path is the only stable handle it offers -- the same rule
    ``import markdown`` already applies.
    """

    key = f"path/{note.relative}"
    permalink = note.properties.get("permalink")
    if dialect == "basic-memory" and isinstance(permalink, str) and permalink.strip():
        key = f"permalink/{permalink.strip()[:512]}"
    note.original_id = (
        f"{dialect.replace('-', '_')}_"
        + hashlib.sha256(key.encode("utf-8")).hexdigest()
    )
    note.entry_id = "kn_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://noetrail.local/import/{dialect}/{key}",
    ).hex


def existing_import_state(
    root: Path, dialect: str
) -> tuple[dict[str, list[dict[str, object]]], set[str]]:
    """Index entries a previous run of this importer already created."""

    by_source: dict[str, list[dict[str, object]]] = {}
    known_ids: set[str] = set()
    for path in managed_paths(root):
        try:
            metadata, _ = parse_frontmatter(path)
        except ValueError:
            continue
        entry_id = metadata.get("id")
        if isinstance(entry_id, str):
            known_ids.add(entry_id)
        source = metadata.get("source")
        if not isinstance(source, dict) or source.get("system") != dialect:
            continue
        original_id = source.get("original_id")
        if isinstance(original_id, str):
            by_source.setdefault(original_id, []).append(metadata)
    return by_source, known_ids


class LossyTotals:
    """Everything the import could not carry over, aggregated per run."""

    def __init__(self) -> None:
        self.dataview = 0
        self.templater = 0
        self.core_templates = 0
        self.dropped_properties: set[str] = set()
        self.substituted_predicates: set[str] = set()
        self.unsupported_embeds: list[str] = []
        self.attachment_problems: list[dict[str, str]] = []
        self.unresolved_targets: list[str] = []
        self.preserved_properties = 0

    def add_note(self, note: SourceNote) -> None:
        self.dataview += note.lossy.dataview
        self.templater += note.lossy.templater
        self.core_templates += note.lossy.core_templates


def _predicate_for(
    declared: str | None, predicates: set[str], totals: LossyTotals
) -> str:
    """Keep the source's own predicate when the instance configures it.

    A Basic Memory vault carries relation types such as ``implements`` or
    ``depends_on``. Accepting them unconditionally would let foreign data
    extend the vault's relation vocabulary, and dropping them silently would
    lose what the edge meant. Adding them to
    ``<config-root>/relation-types.yaml`` is what preserves them; every
    substitution made without that is reported.
    """

    if declared is None:
        return DEFAULT_PREDICATE
    if declared in predicates:
        return declared
    totals.substituted_predicates.add(declared)
    return DEFAULT_PREDICATE


def _attachment_for(
    root: Path,
    note: SourceNote,
    asset: Path,
    source: Path,
    dialect: str,
    timestamp: str,
    existing: list[dict[str, object]],
    totals: LossyTotals,
) -> tuple[dict[str, object], tuple[Path, str, str]] | None:
    if asset.suffix.casefold() not in IMAGE_SUFFIXES:
        totals.unsupported_embeds.append(_posix(asset, source))
        return None
    result = read_vault_image(asset)
    if isinstance(result, str):
        totals.attachment_problems.append(
            {"source": _posix(asset, source), "reason": result}
        )
        return None
    data, media_type, extension = result
    digest = hashlib.sha256(data).hexdigest()
    if any(item.get("sha256") == digest for item in existing):
        return None
    if len(existing) >= MAX_ATTACHMENTS_PER_ENTRY:
        totals.attachment_problems.append(
            {
                "source": _posix(asset, source),
                "reason": "entry attachment limit reached",
            }
        )
        return None
    attachment_id = "ka_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://noetrail.local/import/{dialect}/attachment/"
        f"{note.relative}#{digest}",
    ).hex
    record: dict[str, object] = {
        "id": attachment_id,
        "path": attachment_blob_path(root, digest, extension)
        .relative_to(root)
        .as_posix(),
        "media_type": media_type,
        "sha256": digest,
        "size_bytes": len(data),
        "original_name": normalize_attachment_name(asset.name, extension),
        "added_at": timestamp,
        "origin": "vault_import",
    }
    return record, (asset, digest, extension)


def _preserved_properties(
    note: SourceNote, totals: LossyTotals, dialect: str
) -> dict[str, object]:
    """Keep unmapped user properties verbatim next to the source record.

    The built-in `note` type declares no attributes, so there is no typed slot
    for `status: draft` or `project: Apollo`. Preserving them under
    `source.frontmatter` keeps the information the user wrote without claiming
    a schema for it; anything that would not survive as one JSON value is
    dropped and named in the report.
    """

    # `permalink` is an Obsidian Publish setting, but Basic Memory's stable
    # identifier, where it is recorded on the source record instead.
    mapped = ENVELOPE_PROPERTIES | (
        {"permalink", "type"} if dialect == "basic-memory" else set()
    )
    presentation = OBSIDIAN_PRESENTATION_PROPERTIES - mapped
    preserved: dict[str, object] = {}
    for name, value in note.properties.items():
        if name in mapped:
            continue
        if name.casefold() in presentation:
            # Presentational and Publish-only settings describe how Obsidian
            # renders a note, not what it says, so they are named in the
            # report rather than stored on the entry.
            totals.dropped_properties.add(name)
            continue
        reduced = _storable(value)
        if reduced is None and value is not None:
            totals.dropped_properties.add(name)
            continue
        preserved[name] = reduced
    totals.preserved_properties += len(preserved)
    return preserved


def _build_plan(
    args: argparse.Namespace,
    root: Path,
    source: Path,
    scan: VaultScan,
    note: SourceNote,
    index: LinkIndex,
    predicates: set[str],
    timestamp: str,
    destination: Path,
    dialect: str,
    totals: LossyTotals,
) -> EntryPlan | dict[str, str]:
    relations: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    attachments: list[dict[str, object]] = []
    blobs: list[tuple[Path, str, str]] = []

    for target, embed, declared in note.links:
        resolved = index.resolve(target)
        if resolved == note.entry_id:
            continue
        if resolved is not None:
            relation = {
                "predicate": _predicate_for(declared, predicates, totals),
                "target": resolved,
            }
            if relation not in relations:
                relations.append(relation)
            continue
        asset = resolve_asset(scan, target) if embed else None
        if asset is not None:
            built = _attachment_for(
                root,
                note,
                asset,
                source,
                dialect,
                timestamp,
                attachments,
                totals,
            )
            if built is not None:
                attachments.append(built[0])
                blobs.append(built[1])
            continue
        if embed:
            totals.unsupported_embeds.append(target[:200])
        reference = target.strip()[:200]
        if not reference:
            continue
        candidate = {
            "predicate": _predicate_for(declared, predicates, totals),
            "reference": reference,
        }
        if candidate not in unresolved:
            unresolved.append(candidate)
        totals.unresolved_targets.append(reference)

    relations = relations[:MAX_VAULT_IMPORT_RELATIONS]
    unresolved = unresolved[:MAX_VAULT_IMPORT_UNRESOLVED]
    totals.add_note(note)

    source_record: dict[str, object] = {
        "system": dialect,
        "original_id": note.original_id,
        "imported_at": timestamp,
        "original_path": note.vault_relative,
        "content_sha256": note.digest,
    }
    if note.aliases:
        source_record["aliases"] = note.aliases[:64]
    if dialect == "basic-memory":
        for name, key in (("permalink", "permalink"), ("type", "original_type")):
            value = note.properties.get(name)
            if isinstance(value, str) and value.strip():
                source_record[key] = value.strip()[:512]
    preserved = _preserved_properties(note, totals, dialect)
    if preserved:
        source_record["frontmatter"] = preserved
    if note.observation_categories:
        source_record["observation_categories"] = note.observation_categories[:64]

    tags = normalize_tag_list([*note.tags, *args.tag])
    metadata: dict[str, object] = {
        "id": note.entry_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": "note",
        "title": note.title,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "unreviewed",
        "sensitivity": args.sensitivity,
        "tags": tags,
        "relations": relations,
        "origin": "import",
        "source": source_record,
    }
    if note.aliases:
        metadata["aliases"] = normalize_alias_list(note.aliases[:64], note.title)
    if unresolved:
        metadata["unresolved_relations"] = unresolved
    if attachments:
        metadata["attachments"] = attachments
    metadata = pack_builtin_metadata(metadata, args.registry)
    # The vault holds the user's own writing: not fetched from a page, not
    # produced by a model. `user` is the only one of the three origins that is
    # true here, and `web` would make every search result flag the user's own
    # words as untrusted network content.
    origins = {"title": "user"}
    if tags:
        origins["tags"] = "user"
    metadata = with_provenance(metadata, origins)

    filename = (
        f"{datetime.now().astimezone().date()}--{slugify(note.title)}--"
        f"{note.entry_id[3:11]}.md"
    )
    target_path = destination / filename
    if target_path.exists():
        return {"source": note.vault_relative, "reason": "destination exists"}
    return EntryPlan(
        source=note.vault_relative,
        entry_id=note.entry_id,
        title=note.title,
        target=target_path,
        metadata=metadata,
        body=note.body,
        blobs=blobs,
        relation_count=len(relations),
        unresolved_count=len(unresolved),
        attachment_count=len(attachments),
    )


def _apply(root: Path, plans: list[EntryPlan], destination: Path) -> None:
    """Write every planned entry and blob, or leave the vault untouched."""

    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    stored: list[Path] = []
    try:
        for plan in plans:
            for asset, digest, extension in plan.blobs:
                blob = attachment_blob_path(root, digest, extension)
                fresh = not blob.exists()
                result = read_vault_image(asset)
                if isinstance(result, str) or (
                    hashlib.sha256(result[0]).hexdigest() != digest
                ):
                    raise StorageError(
                        "Embedded image changed between preflight and apply: "
                        f"{asset.name}"
                    )
                write_attachment_blob(root, result[0], digest, extension)
                if fresh:
                    stored.append(blob)
            atomic_write_entry(
                plan.target,
                plan.metadata,
                plan.body,
                must_not_exist=True,
            )
            written.append(plan.target)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        # Only blobs this run created: a shared blob an earlier entry already
        # references must survive a rollback of this one.
        for blob in stored:
            blob.unlink(missing_ok=True)
        raise


def command_import_vault(args: argparse.Namespace, root: Path, dialect: str) -> int:
    if args.limit is not None and (
        args.limit < 1 or args.limit > MAX_MARKDOWN_IMPORT_FILES
    ):
        raise InvalidRequest(f"--limit must be from 1 to {MAX_MARKDOWN_IMPORT_FILES}")
    raw_root, source = markdown_import_source(root, args.source)
    if not source.is_dir():
        raise InvalidRequest("Vault import source must be a directory")

    scan = scan_vault(source)
    total_markdown = len(scan.markdown)
    selected = scan.markdown if args.limit is None else scan.markdown[: args.limit]

    rejected: list[dict[str, str]] = [
        {"source": relative, "reason": "symbolic link"} for relative in scan.symlinks
    ]
    notes: list[SourceNote] = []
    for path in selected:
        parsed = parse_note(
            path, _posix(path, raw_root), _posix(path, source), dialect
        )
        if isinstance(parsed, dict):
            rejected.append(parsed)
            continue
        identify(parsed, dialect)
        notes.append(parsed)

    by_source, known_ids = existing_import_state(root, dialect)
    errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    importable: list[SourceNote] = []
    index = LinkIndex()
    for note in notes:
        existing = by_source.get(note.original_id, [])
        if len(existing) > 1:
            errors.append(
                {
                    "source": note.vault_relative,
                    "reason": "source ID is duplicated in the vault",
                }
            )
            continue
        if existing:
            stored_source = existing[0].get("source")
            stored_digest = (
                stored_source.get("content_sha256")
                if isinstance(stored_source, dict)
                else None
            )
            if stored_digest is not None and stored_digest != note.digest:
                errors.append(
                    {
                        "source": note.vault_relative,
                        "reason": "raw source changed after its previous import",
                    }
                )
                continue
            entry_id = str(existing[0].get("id", ""))
            skipped.append(
                {
                    "source": note.vault_relative,
                    "id": entry_id,
                    "reason": "already_imported",
                }
            )
            index.add(note.vault_relative, entry_id, note.link_names)
            continue
        if note.entry_id in known_ids:
            errors.append(
                {
                    "source": note.vault_relative,
                    "reason": "derived entry ID already exists",
                }
            )
            continue
        importable.append(note)
        index.add(note.vault_relative, note.entry_id, note.link_names)

    predicates = load_relation_types(args.layout)
    timestamp = now_iso()
    destination = entry_directory(root, "note")
    totals = LossyTotals()
    plans: list[EntryPlan] = []
    for note in importable:
        built = _build_plan(
            args,
            root,
            source,
            scan,
            note,
            index,
            predicates,
            timestamp,
            destination,
            dialect,
            totals,
        )
        if isinstance(built, dict):
            errors.append(built)
            continue
        plans.append(built)

    report: dict[str, object] = {
        "dry_run": not args.apply,
        "system": dialect,
        "source": _posix(source, raw_root),
        "counts": {
            "markdown_files": total_markdown,
            "selected": len(selected),
            "planned": len(plans),
            "skipped": len(skipped),
            "rejected": len(rejected),
            "relations": sum(plan.relation_count for plan in plans),
            "unresolved_relations": sum(plan.unresolved_count for plan in plans),
            "attachments": sum(plan.attachment_count for plan in plans),
            "canvas_files": len(scan.canvas),
            "unsupported_files": len(scan.other_files),
            "preserved_properties": totals.preserved_properties,
            "total_files": scan.total_files,
        },
        "truncated": len(selected) < total_markdown,
        "not_imported": {
            "canvas": _bounded(scan.canvas),
            "unsupported_files": _bounded(scan.other_files),
            "unsupported_embeds": _bounded(totals.unsupported_embeds),
            "attachment_problems": sorted(
                totals.attachment_problems, key=lambda item: item["source"]
            )[:MAX_VAULT_IMPORT_REPORT_ITEMS],
            "dropped_properties": _bounded(sorted(totals.dropped_properties)),
            "dataview_blocks": totals.dataview,
            "templater_expressions": totals.templater,
            "core_template_placeholders": totals.core_templates,
            "substituted_relation_types": _bounded(
                sorted(totals.substituted_predicates)
            ),
        },
        "ignored": {
            "obsidian_config": scan.config_present,
            "hidden_entries": _bounded(scan.ignored_hidden),
        },
        "unresolved_link_targets": _bounded(totals.unresolved_targets),
        "rejected": sorted(rejected, key=lambda item: item["source"])[
            :MAX_VAULT_IMPORT_REPORT_ITEMS
        ],
        "skipped": skipped[:MAX_VAULT_IMPORT_REPORT_ITEMS],
        "created": [],
        "errors": [],
    }

    if errors or (args.strict and rejected):
        if args.strict and rejected and not errors:
            errors = [
                {
                    "source": "--strict",
                    "reason": f"{len(rejected)} files were rejected",
                }
            ]
        report["errors"] = sorted(errors, key=lambda item: item["source"])[
            :MAX_VAULT_IMPORT_REPORT_ITEMS
        ]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    if args.apply:
        _apply(root, plans, destination)

    report["created"] = [
        {
            "source": plan.source,
            "id": plan.entry_id,
            "title": plan.title,
            "created": str(plan.target.relative_to(root)),
            "relations": plan.relation_count,
            "unresolved_relations": plan.unresolved_count,
            "attachments": plan.attachment_count,
        }
        for plan in plans[:MAX_VAULT_IMPORT_REPORT_ITEMS]
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_import_obsidian(args: argparse.Namespace, root: Path) -> int:
    """Import an Obsidian vault copied below imports/raw."""

    return command_import_vault(args, root, "obsidian")


def command_import_basic_memory(args: argparse.Namespace, root: Path) -> int:
    """Import a Basic Memory project directory copied below imports/raw."""

    return command_import_vault(args, root, "basic-memory")

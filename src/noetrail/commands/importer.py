#!/usr/bin/env python3
"""The `import markdown` command."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from noetrail.constants import MAX_MARKDOWN_IMPORT_BYTES, MAX_MARKDOWN_IMPORT_FILES
from noetrail.errors import (
    InternalError,
    InvalidRequest,
    StorageError,
)
from noetrail.frontmatter import pack_builtin_metadata, parse_frontmatter
from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
)
from noetrail.normalize import normalize_tag_list, now_iso, slugify
from noetrail.store import atomic_write_entry, entry_directory, managed_paths


def markdown_import_source(root: Path, source_value: str) -> tuple[Path, Path]:
    """Resolve one Markdown import source without leaving imports/raw."""

    raw_root = root / "imports" / "raw"
    try:
        resolved_raw = raw_root.resolve(strict=True)
    except OSError as exc:
        raise StorageError("Markdown import root imports/raw is unavailable") from exc
    source = Path(source_value).expanduser()
    candidate = source if source.is_absolute() else resolved_raw / source
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(resolved_raw)
    except (OSError, ValueError) as exc:
        raise InvalidRequest(
            "Markdown import source must be below imports/raw"
        ) from exc

    current = resolved_raw
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise InvalidRequest(
                "Markdown import source must not contain symbolic links"
            )
    if not resolved.is_file() and not resolved.is_dir():
        raise InvalidRequest(
            "Markdown import source must be a regular file or directory"
        )
    return resolved_raw, resolved


def markdown_import_paths(source: Path, limit: int | None) -> tuple[list[Path], int]:
    if source.is_file():
        if source.suffix.casefold() != ".md":
            raise InvalidRequest(
                "Markdown import source file must use the .md extension"
            )
        candidates = [source]
    else:
        descendants = sorted(source.rglob("*"))
        symbolic = [path for path in descendants if path.is_symlink()]
        if symbolic:
            raise InvalidRequest(
                "Markdown import source must not contain symbolic links"
            )
        candidates = [
            path
            for path in descendants
            if path.is_file() and path.suffix.casefold() == ".md"
        ]
    if len(candidates) > MAX_MARKDOWN_IMPORT_FILES:
        raise InvalidRequest(
            f"Markdown import contains more than {MAX_MARKDOWN_IMPORT_FILES} files"
        )
    total = len(candidates)
    return (candidates[:limit] if limit is not None else candidates), total


def read_markdown_import(path: Path) -> tuple[str, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("file could not be opened") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("source is not a regular file")
        if details.st_size > MAX_MARKDOWN_IMPORT_BYTES:
            raise ValueError(
                f"file exceeds the {MAX_MARKDOWN_IMPORT_BYTES}-byte limit"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(MAX_MARKDOWN_IMPORT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > MAX_MARKDOWN_IMPORT_BYTES:
        raise ValueError(f"file exceeds the {MAX_MARKDOWN_IMPORT_BYTES}-byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not valid UTF-8") from exc
    if not text.strip():
        raise ValueError("file is empty")
    if "\x00" in text:
        raise ValueError("file contains a NUL byte")
    return text, hashlib.sha256(data).hexdigest()


def markdown_import_title(path: Path, text: str) -> str:
    for line in text.splitlines():
        match = re.fullmatch(r"#\s+(.+?)\s*", line)
        if match:
            title = match.group(1).strip().strip("#").strip()
            if title:
                return title[:500]
    fallback = re.sub(r"[_-]+", " ", path.stem).strip()
    return (fallback or "Imported Markdown note")[:500]


def command_import_markdown(args: argparse.Namespace, root: Path) -> int:
    if args.limit is not None and (
        args.limit < 1 or args.limit > MAX_MARKDOWN_IMPORT_FILES
    ):
        raise InvalidRequest(
            f"--limit must be from 1 to {MAX_MARKDOWN_IMPORT_FILES}"
        )
    raw_root, source = markdown_import_source(root, args.source)
    paths, total_candidates = markdown_import_paths(source, args.limit)

    existing_by_source: dict[str, list[tuple[Path, dict[str, object]]]] = {}
    existing_by_id: dict[str, Path] = {}
    for existing_path in managed_paths(root):
        try:
            metadata, _ = parse_frontmatter(existing_path)
        except ValueError:
            continue
        entry_id = metadata.get("id")
        if isinstance(entry_id, str):
            existing_by_id[entry_id] = existing_path
        existing_source = metadata.get("source")
        if not isinstance(existing_source, dict):
            continue
        if existing_source.get("system") != "markdown":
            continue
        original_id = existing_source.get("original_id")
        if isinstance(original_id, str):
            existing_by_source.setdefault(original_id, []).append(
                (existing_path, metadata)
            )

    errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    plans: list[dict[str, object]] = []
    timestamp = now_iso()
    destination = entry_directory(root, "note")

    for path in paths:
        relative = path.relative_to(raw_root).as_posix()
        try:
            text, digest = read_markdown_import(path)
        except ValueError as exc:
            errors.append({"source": relative, "reason": str(exc)})
            continue
        original_id = "md_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()
        existing = existing_by_source.get(original_id, [])
        if len(existing) > 1:
            errors.append(
                {"source": relative, "reason": "source ID is duplicated in the vault"}
            )
            continue
        if existing:
            existing_path, existing_metadata = existing[0]
            existing_source = existing_metadata.get("source")
            stored_digest = (
                existing_source.get("content_sha256")
                if isinstance(existing_source, dict)
                else None
            )
            if stored_digest is not None and stored_digest != digest:
                errors.append(
                    {
                        "source": relative,
                        "reason": "raw source changed after its previous import",
                    }
                )
                continue
            skipped.append(
                {
                    "source": relative,
                    "id": str(existing_metadata.get("id", "")),
                    "reason": "already_imported",
                }
            )
            continue

        entry_id = "kn_" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"https://noetrail.local/import/markdown/{relative}",
        ).hex
        if entry_id in existing_by_id:
            errors.append(
                {"source": relative, "reason": "derived entry ID already exists"}
            )
            continue
        title = markdown_import_title(path, text)
        planned_metadata: dict[str, object] = {
            "id": entry_id,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "type": "note",
            "title": title,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": "unreviewed",
            "sensitivity": args.sensitivity,
            "tags": normalize_tag_list(args.tag),
            "relations": [],
            "origin": "import",
            "source": {
                "system": "markdown",
                "original_id": original_id,
                "imported_at": timestamp,
                "original_path": relative,
                "content_sha256": digest,
            },
        }
        planned_metadata = pack_builtin_metadata(planned_metadata, args.registry)
        filename = (
            f"{datetime.now().astimezone().date()}--{slugify(title)}--"
            f"{entry_id[3:11]}.md"
        )
        target = destination / filename
        if target.exists():
            errors.append({"source": relative, "reason": "destination exists"})
            continue
        plans.append(
            {
                "source": relative,
                "id": entry_id,
                "title": title,
                "target": target,
                "metadata": planned_metadata,
                "body": text,
            }
        )

    if errors:
        print(
            json.dumps(
                {
                    "dry_run": not args.apply,
                    "candidate_count": len(paths),
                    "total_candidate_count": total_candidates,
                    "planned_count": len(plans),
                    "skipped": skipped,
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    created: list[dict[str, str]] = []
    written: list[Path] = []
    if args.apply:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            for plan in plans:
                plan_target = plan["target"]
                plan_metadata = plan["metadata"]
                plan_body = plan["body"]
                if not isinstance(plan_target, Path) or not isinstance(
                    plan_metadata, dict
                ):
                    raise InternalError("Invalid internal Markdown import plan")
                if not isinstance(plan_body, str):
                    raise InternalError("Invalid internal Markdown import body")
                atomic_write_entry(
                    plan_target,
                    plan_metadata,
                    plan_body,
                    must_not_exist=True,
                )
                written.append(plan_target)
        except BaseException:
            for written_path in written:
                written_path.unlink(missing_ok=True)
            raise
    for plan in plans:
        plan_target = plan["target"]
        if not isinstance(plan_target, Path):
            raise InternalError("Invalid internal Markdown import target")
        created.append(
            {
                "source": str(plan["source"]),
                "id": str(plan["id"]),
                "title": str(plan["title"]),
                "created": str(plan_target.relative_to(root)),
            }
        )

    print(
        json.dumps(
            {
                "dry_run": not args.apply,
                "candidate_count": len(paths),
                "total_candidate_count": total_candidates,
                "truncated": len(paths) < total_candidates,
                "created": created,
                "skipped": skipped,
                "errors": [],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0

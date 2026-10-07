#!/usr/bin/env python3
"""The review and lifecycle commands: review, trash, restore, purge."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path

from noetrail.body import LEGACY_BODY_HEADINGS, heading_for
from noetrail.constants import ACTIVE_STATUSES, WEB_CONTENT_BEGIN, WEB_CONTENT_END
from noetrail.deadline import check_deadline
from noetrail.entries import (
    find_entry,
    find_trashed_entry,
    load_trashed_entries,
    timestamp_sort_key,
)
from noetrail.errors import (
    Conflict,
    InvalidRequest,
    ValidationError,
)
from noetrail.frontmatter import (
    is_current_schema,
    require_current_schema,
    require_supported_entry_type,
)
from noetrail.normalize import now_iso
from noetrail.provenance import provenance_report, search_provenance
from noetrail.store import (
    atomic_write_entry,
    parse_timestamp,
    relocate_entry,
    require_expected_revision,
    revision_for,
    safe_restore_path,
)
from noetrail.temporal import relations_at, resolve_as_of


def bookmark_has_personal_note(
    body: str, headings: dict[str, str] | None = None
) -> bool:
    wanted = {
        f"## {heading_for(headings, 'personal_note')}".casefold(),
        *(
            f"## {legacy}".casefold()
            for legacy in LEGACY_BODY_HEADINGS["personal_note"]
        ),
    }
    lines = body.splitlines()
    for index, line in enumerate(lines):
        check_deadline()
        if line.strip().casefold() not in wanted:
            continue
        section: list[str] = []
        for candidate in lines[index + 1 :]:
            check_deadline()
            if candidate.startswith("## "):
                break
            # The fence opening the fetched section sits above its heading, so
            # it lands inside the personal note's lines. Counting it as content
            # would report an empty note as written.
            if candidate.strip() in {WEB_CONTENT_BEGIN, WEB_CONTENT_END}:
                continue
            section.append(candidate)
        if "\n".join(section).strip():
            return True
    return False


def review_reasons(
    metadata: dict[str, object],
    body: str,
    headings: dict[str, str] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if metadata.get("status") == "unreviewed":
        reasons.append("unreviewed")
    unresolved = metadata.get("unresolved_relations", [])
    if isinstance(unresolved, list) and unresolved:
        reasons.append("unresolved_relations")
    if (
        metadata.get("type") == "bookmark"
        and not bookmark_has_personal_note(body, headings)
        and "reviewed_at" not in metadata
    ):
        reasons.append("bookmark_missing_personal_note")
    return reasons


def review_summary(
    root: Path,
    path: Path,
    metadata: dict[str, object],
    body: str,
    headings: dict[str, str] | None = None,
) -> dict[str, object]:
    attachments = metadata.get("attachments", [])
    attachment_count = len(attachments) if isinstance(attachments, list) else 0
    summary: dict[str, object] = {
        "id": metadata.get("id"),
        "type": metadata.get("type"),
        "title": metadata.get("title"),
        "path": str(path.relative_to(root)),
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "revision": revision_for(path),
        "sensitivity": metadata.get("sensitivity"),
        "reasons": review_reasons(metadata, body, headings),
        "unresolved_relations": metadata.get("unresolved_relations", []),
        "attachment_count": attachment_count,
    }
    if "type_version" in metadata:
        summary["type_version"] = metadata.get("type_version")
    if "attributes" in metadata:
        summary["attributes"] = metadata.get("attributes")
    if metadata.get("aliases"):
        summary["aliases"] = metadata.get("aliases")
    provenance = search_provenance(metadata, body, headings)
    if provenance is not None:
        summary["provenance"] = provenance
    return summary


def entry_detail(
    root: Path,
    path: Path,
    metadata: dict[str, object],
    body: str,
    headings: dict[str, str] | None = None,
    as_of: str | None = None,
) -> dict[str, object]:
    """Return the canonical full read representation for one active entry."""

    require_current_schema(metadata)
    as_of_instant, as_of_label = resolve_as_of(as_of)
    current, not_in_force = relations_at(metadata, as_of_instant)
    return {
        **review_summary(root, path, metadata, body, headings),
        **({"as_of": as_of_label} if as_of_label else {}),
        "relations": current,
        "relations_not_in_force": not_in_force,
        "provenance": provenance_report(metadata, body, headings),
        "body": body,
        "attachments": metadata.get("attachments", []),
    }


def command_review(args: argparse.Namespace, root: Path) -> int:
    if args.id is None:
        if args.complete:
            raise InvalidRequest("--complete requires an entry ID")
        if args.as_of is not None:
            # The queue is a list of entries needing work, not a statement
            # about what held when; answering it "as of" a past instant would
            # suggest a history it does not keep.
            raise InvalidRequest("--as-of requires an entry ID")
        if args.limit < 1:
            raise InvalidRequest("--limit must be at least 1")
        queue = []
        # A single entry left on an older schema used to abort this *read*
        # command outright, which combined badly with `migrate`: migrating
        # could damage an entry, and not migrating blocked the review queue.
        # Legacy entries are skipped and reported instead.
        skipped_legacy: list[str] = []
        for path, metadata, body in args.index.entries:
            check_deadline()
            if not is_current_schema(metadata):
                skipped_legacy.append(str(path.relative_to(root)))
                continue
            reasons = review_reasons(metadata, body, args.body_headings)
            if reasons:
                queue.append(
                    review_summary(root, path, metadata, body, args.body_headings)
                )
        queue.sort(key=lambda item: str(item.get("id", "")))
        queue.sort(key=lambda item: timestamp_sort_key(item, "created_at"))
        total_count = len(queue)
        queue = queue[: args.limit]
        print(
            json.dumps(
                {
                    "count": total_count,
                    "displayed": len(queue),
                    "complete": not skipped_legacy,
                    "skipped_legacy": sorted(skipped_legacy),
                    "items": queue,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    reasons = review_reasons(metadata, body, args.body_headings)
    if not args.complete:
        # `body` stays the whole file. The origin of every part of it is
        # reported next to it instead, with line spans, so a client can split
        # trusted from fetched text without parsing Markdown -- and without
        # this command handing back a body that no longer matches the file,
        # which `update --replace-body` would then write out truncated.
        print(
            json.dumps(
                entry_detail(
                    root,
                    path,
                    metadata,
                    body,
                    args.body_headings,
                    args.as_of,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not reasons:
        raise InvalidRequest("Entry has no open review reason")
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)
    unresolved = metadata.get("unresolved_relations", [])
    if isinstance(unresolved, list) and unresolved:
        raise InvalidRequest(
            "Resolve or remove unresolved relations before completing review"
        )

    timestamp = now_iso()
    changes: dict[str, object] = {"reviewed_at": timestamp}
    metadata["reviewed_at"] = timestamp
    if metadata.get("status") == "unreviewed":
        metadata["status"] = "active"
        changes["status"] = "active"
    metadata["updated_at"] = timestamp
    atomic_write_entry(path, metadata, body)
    print(
        json.dumps(
            {
                "reviewed": str(path.relative_to(root)),
                "id": args.id,
                "title": metadata.get("title"),
                "changes": changes,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_trash(args: argparse.Namespace, root: Path) -> int:
    if args.target == "list":
        entries = []
        for path, metadata, _ in load_trashed_entries(root):
            check_deadline()
            entries.append(
                {
                    "id": metadata.get("id"),
                    "type": metadata.get("type"),
                    "title": metadata.get("title"),
                    "deleted_at": metadata.get("deleted_at"),
                    "purge_after": metadata.get("purge_after"),
                    "deleted_from": metadata.get("deleted_from"),
                    "path": str(path.relative_to(root)),
                    "updated_at": metadata.get("updated_at"),
                    "revision": revision_for(path),
                    "sensitivity": metadata.get("sensitivity"),
                }
            )
        entries.sort(key=lambda item: str(item.get("id", "")))
        entries.sort(
            key=lambda item: timestamp_sort_key(item, "deleted_at"),
            reverse=True,
        )
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0

    if args.retention_days < 1:
        raise InvalidRequest("--retention-days must be at least 1")
    path, metadata, body = find_entry(args.index, args.target)
    require_current_schema(metadata)
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)
    previous_status = metadata.get("status")
    if previous_status not in ACTIVE_STATUSES:
        raise ValidationError(f"Entry has invalid active status: {previous_status}")

    deleted_at = datetime.now().astimezone()
    relative_source = path.relative_to(root)
    destination = (
        root
        / "trash"
        / f"{deleted_at.year:04d}"
        / f"{deleted_at.month:02d}"
        / relative_source
    )
    metadata["previous_status"] = previous_status
    metadata["status"] = "trashed"
    metadata["deleted_at"] = deleted_at.isoformat(timespec="microseconds")
    metadata["deleted_from"] = relative_source.as_posix()
    metadata["purge_after"] = (
        deleted_at + timedelta(days=args.retention_days)
    ).isoformat(timespec="microseconds")
    if args.reason:
        metadata["deletion_reason"] = args.reason
    metadata["updated_at"] = metadata["deleted_at"]

    if destination.exists():
        raise Conflict(
            f"Trash target already exists; no files were changed: "
            f"{destination.relative_to(root)}"
        )
    relocate_entry(path, destination, metadata, body)
    print(
        json.dumps(
            {
                "trashed": str(destination.relative_to(root)),
                "deleted_from": str(relative_source),
                "id": args.target,
                "title": metadata.get("title"),
                "previous_status": previous_status,
                "purge_after": metadata["purge_after"],
                "revision": revision_for(destination),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_restore(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_trashed_entry(root, args.id)
    require_current_schema(metadata)
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)
    if metadata.get("status") != "trashed":
        raise ValidationError("Trash entry does not have status trashed")
    previous_status = metadata.get("previous_status")
    if previous_status not in ACTIVE_STATUSES:
        raise ValidationError("Trash entry has no valid previous_status")
    destination = safe_restore_path(root, metadata.get("deleted_from"))
    if destination.exists():
        raise Conflict(
            f"Restore target already exists; no files were changed: "
            f"{destination.relative_to(root)}"
        )

    metadata["status"] = previous_status
    for field in (
        "previous_status",
        "deleted_at",
        "deleted_from",
        "purge_after",
        "deletion_reason",
    ):
        check_deadline()
        metadata.pop(field, None)
    metadata["updated_at"] = now_iso()

    relocate_entry(path, destination, metadata, body)
    print(
        json.dumps(
            {
                "restored": str(destination.relative_to(root)),
                "id": args.id,
                "title": metadata.get("title"),
                "status": previous_status,
                "revision": revision_for(destination),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_purge(args: argparse.Namespace, root: Path) -> int:
    now = datetime.now().astimezone()
    candidates: list[tuple[Path, dict[str, object]]] = []
    entries = load_trashed_entries(root)

    if args.id:
        path, metadata, _ = find_trashed_entry(root, args.id)
        require_current_schema(metadata)
        if metadata.get("status") != "trashed":
            raise InvalidRequest("Refusing to purge an entry without status trashed")
        candidates.append((path, metadata))
    else:
        if args.older_than_days is not None and args.older_than_days < 0:
            raise InvalidRequest("--older-than-days cannot be negative")
        for path, metadata, _ in entries:
            check_deadline()
            require_current_schema(metadata)
            if metadata.get("status") != "trashed":
                raise ValidationError(
                    f"Refusing to purge non-trashed file: {path.relative_to(root)}"
                )
            if args.older_than_days is not None:
                deleted_at = parse_timestamp(metadata.get("deleted_at"), "deleted_at")
                selected = deleted_at <= now - timedelta(days=args.older_than_days)
            else:
                purge_after = parse_timestamp(
                    metadata.get("purge_after"), "purge_after"
                )
                selected = purge_after <= now
            if selected:
                candidates.append((path, metadata))

    results = [
        {
            "id": metadata.get("id"),
            "title": metadata.get("title"),
            "path": str(path.relative_to(root)),
            "deleted_at": metadata.get("deleted_at"),
            "purge_after": metadata.get("purge_after"),
        }
        for path, metadata in candidates
    ]
    if args.apply:
        for path, _ in candidates:
            check_deadline()
            path.unlink()

    print(
        json.dumps(
            {
                "dry_run": not args.apply,
                "purged": results if args.apply else [],
                "candidates": [] if args.apply else results,
                "backup_copies_unchanged": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0

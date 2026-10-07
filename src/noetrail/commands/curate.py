"""Explicit knowledge maintenance: candidate suggestions and reviewed merges."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile

from noetrail.body import body_has_web_content
from noetrail.deadline import check_deadline
from noetrail.entries import find_entry
from noetrail.errors import Conflict, InvalidRequest, RevisionConflict, ValidationError
from noetrail.frontmatter import metadata_list, parse_frontmatter, runtime_metadata
from noetrail.normalize import name_key, normalize_alias_list, normalize_tag_list
from noetrail.relations import load_relation_types
from noetrail.search import tokenize
from noetrail.store import atomic_write_entry, relocate_entry, revision_for
from noetrail.validation import validate_metadata


def command_candidates(args: argparse.Namespace, root: Path) -> int:
    """Compare one selected entry, never all pairs in a vault."""
    _, source, _ = find_entry(args.index, args.id)
    names = {
        name_key(str(value))
        for value in [source.get("title", ""), *metadata_list(source, "aliases")]
    }
    terms = set(tokenize(str(source.get("title", ""))))
    candidates = []
    for path, metadata, _ in args.index.entries:
        check_deadline()
        if metadata.get("id") == args.id:
            continue
        other_names = {
            name_key(str(value))
            for value in [
                metadata.get("title", ""),
                *metadata_list(metadata, "aliases"),
            ]
        }
        reasons = []
        if names & other_names:
            reasons.append("shared_name")
        for field in ("canonical_url", "source_url"):
            if source.get(field) and source.get(field) == metadata.get(field):
                reasons.append("same_" + field)
        overlap = terms & set(tokenize(str(metadata.get("title", ""))))
        if not reasons and len(overlap) >= 2:
            reasons.append("shared_title_terms")
        if reasons:
            candidates.append(
                {
                    "id": metadata.get("id"),
                    "title": metadata.get("title"),
                    "type": metadata.get("type"),
                    "reasons": reasons,
                    "revision": revision_for(path),
                }
            )
    candidates.sort(key=lambda item: str(item["id"]))
    candidates.sort(key=lambda item: item["reasons"] == ["shared_title_terms"])
    if not 1 <= args.limit <= 50 or args.offset < 0:
        raise InvalidRequest("limit must be 1..50 and offset non-negative")
    page = candidates[args.offset : args.offset + args.limit]
    next_offset = args.offset + len(page)
    print(
        json.dumps(
            {
                "source_id": args.id,
                "total": len(candidates),
                "items": page,
                "complete": not args.index.unreadable,
                "unreadable_entry_count": len(args.index.unreadable),
                "next_offset": next_offset if next_offset < len(candidates) else None,
                "suggestions_only": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _strings(metadata: dict[str, object], key: str) -> list[str]:
    value = metadata.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{key} must contain strings; run validate")
    return value


def _union(left: object, right: object) -> list:
    if not isinstance(left, list) or not isinstance(right, list):
        raise ValidationError("Merge list field is malformed; run validate")
    result = deepcopy(left)
    result.extend(value for value in right if value not in result)
    return result


def _redirect(metadata: dict, source: str, target: str) -> dict:
    result = deepcopy(metadata)
    for edge in result.get("relations", []):
        if not isinstance(edge, dict):
            raise ValidationError("Malformed relation; run validate")
        for key in ("target", "superseded_by"):
            if edge.get(key) == source:
                edge[key] = target
        if edge.get("target") == result.get("id"):
            raise Conflict("Merge would create a self-relation; resolve it first")
    return result


def command_merge(args: argparse.Namespace, root: Path) -> int:
    """Preserve both identities; move the source to reversible trash last.

    Conflicting metadata is refused rather than guessed. A plan digest covers
    the complete active snapshot, including incoming edges, and must be echoed
    on apply. No new persisted fields or migration are needed.
    """
    if args.source_id == args.target_id:
        raise InvalidRequest("Source and target must be different")
    source_path, _, _ = find_entry(args.index, args.source_id)
    target_path, _, _ = find_entry(args.index, args.target_id)
    if args.index.unreadable:
        raise ValidationError(
            "Unreadable entries could hide incoming edges; run validate"
        )
    source, source_body = parse_frontmatter(source_path)
    target, target_body = parse_frontmatter(target_path)
    if f"## Merged from {args.source_id}" in target_body:
        raise Conflict("Source text was already merged; inspect before retrying")
    if any(len(entries) != 1 for entries in args.index.by_id.values()):
        raise Conflict("Duplicate entry IDs prevent a safe merge; run validate")
    # Fetch-derived bodies cannot be concatenated safely across legacy heading
    # boundaries. Keep those merges explicit manual edits for now.
    if any(
        body_has_web_content(body, args.body_headings) or metadata.get("provenance")
        for metadata, body in ((source, source_body), (target, target_body))
    ):
        raise Conflict(
            "Merge requires entries without provenance or fetched body sections"
        )
    if source.get("schema_version") != 12 or target.get("schema_version") != 12:
        raise Conflict("Merge requires schema 12; migrate explicitly first")
    combined = deepcopy(target)
    combined_lists = (
        "tags",
        "aliases",
        "attachments",
        "relations",
        "unresolved_relations",
    )
    identity = {"id", "title", "created_at", "updated_at", "status", "reviewed_at"}
    conflicts = sorted(
        key
        for key in set(source) | set(target)
        if key not in identity
        and key not in combined_lists
        and source.get(key) != target.get(key)
    )
    if conflicts:
        raise Conflict(
            "Resolve differing metadata before merge: " + ", ".join(conflicts)
        )
    for key in combined_lists:
        values = _union(target.get(key, []), source.get(key, []))
        if values:
            combined[key] = values
    combined["tags"] = normalize_tag_list(_strings(combined, "tags"))
    try:
        aliases = normalize_alias_list(
            _strings(combined, "aliases"), str(target["title"])
        )
    except ValueError as exc:
        raise Conflict(str(exc)) from exc
    if aliases:
        combined["aliases"] = aliases
    combined["status"] = "unreviewed"
    combined.pop("reviewed_at", None)
    combined = _redirect(combined, args.source_id, args.target_id)
    body = (
        target_body.rstrip() + f"\n\n## Merged from {args.source_id}\n\n" + source_body
    )
    changes = [(target_path, combined, body)]
    for path, metadata, entry_body in args.index.entries:
        if path in (source_path, target_path):
            continue
        edges = metadata.get("relations", [])
        if any(
            isinstance(edge, dict)
            and args.source_id in (edge.get("target"), edge.get("superseded_by"))
            for edge in edges
        ):
            raw, entry_body = parse_frontmatter(path)
            changes.append(
                (path, _redirect(raw, args.source_id, args.target_id), entry_body)
            )
    predicates = load_relation_types(args.layout)
    for path, metadata, _ in [(source_path, source, source_body), *changes]:
        errors = validate_metadata(
            path.relative_to(root),
            runtime_metadata(metadata),
            predicates,
            args.registry,
            in_trash=False,
        )
        if errors:
            raise Conflict("Merge validation failed: " + "; ".join(errors))
    snapshot = sorted(
        (str(path.relative_to(root)), revision_for(path))
        for path, _, _ in args.index.entries
    )
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                [args.source_id, args.target_id, snapshot], separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    plan = {
        "dry_run": not args.apply,
        "source_id": args.source_id,
        "target_id": args.target_id,
        "expected_plan": digest,
        "updated_ids": [metadata["id"] for _, metadata, _ in changes],
        "target_metadata": combined,
        "target_body": body,
        "source_action": "trash",
        "source_retained_days": 90,
    }
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.expected_plan != digest:
        raise RevisionConflict(
            "Merge plan changed or is missing; preview again before apply"
        )
    now = datetime.now().astimezone()
    timestamp = now.isoformat(timespec="microseconds")
    destination = (
        root
        / "trash"
        / f"{now.year:04d}"
        / f"{now.month:02d}"
        / source_path.relative_to(root)
    )
    if destination.exists():
        raise Conflict("Merge trash destination already exists")
    trashed = deepcopy(source)
    trashed.update(
        {
            "status": "trashed",
            "previous_status": source["status"],
            "deleted_at": timestamp,
            "updated_at": timestamp,
            "deleted_from": source_path.relative_to(root).as_posix(),
            "purge_after": (now + timedelta(days=90)).isoformat(
                timespec="microseconds"
            ),
            "deletion_reason": f"Merged into {args.target_id}",
        }
    )
    # Preserve exact original bytes, permissions and bodies for rollback on
    # exceptions. Source removal is last, so interruption cannot discard it.
    backups: dict[Path, Path] = {}
    try:
        for path in [source_path, *(path for path, _, _ in changes)]:
            fd, name = tempfile.mkstemp(prefix=".merge-", dir=path.parent)
            backup = Path(name)
            backups[path] = backup
            with os.fdopen(fd, "wb") as handle:
                handle.write(path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(backup, path.stat().st_mode & 0o777)
        try:
            for path, metadata, entry_body in changes:
                metadata["updated_at"] = timestamp
                atomic_write_entry(path, metadata, entry_body)
            relocate_entry(source_path, destination, trashed, source_body)
        except BaseException:
            for path, backup in backups.items():
                os.replace(backup, path)
            destination.unlink(missing_ok=True)
            raise
    finally:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
    plan["revision"] = revision_for(target_path)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0

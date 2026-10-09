"""Revision-checked, fill-only application of an isolated fetcher envelope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from noetrail.entries import find_entry
from noetrail.errors import InvalidRequest, UnsupportedSchema
from noetrail.frontmatter import get_schema_version, require_current_schema
from noetrail.migrations import WEB_METADATA_FIELDS
from noetrail.normalize import canonicalize_url, now_iso
from noetrail.provenance import entry_provenance, with_provenance
from noetrail.schema import SchemaPackError, SchemaRegistry
from noetrail.store import atomic_write_entry, require_expected_revision, revision_for

MAX_ENVELOPE_BYTES = 256 * 1024
RETRIEVAL_STATUSES = ("complete", "partial", "blocked", "failed")


def _object(value: object, keys: set[str], required: set[str], name: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidRequest(f"{name} must be an object")
    if set(value) - keys or required - set(value):
        raise InvalidRequest(f"{name} has unknown or missing fields")
    return value


def _url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        raise InvalidRequest("Fetcher URLs must be strings of at most 4096 characters")
    try:
        return canonicalize_url(value)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def load_envelope(path: str, registry: SchemaRegistry) -> tuple[dict, dict]:
    """Validate the whole untrusted reply before touching an entry."""
    try:
        with Path(path).expanduser().open("rb") as stream:
            raw = stream.read(MAX_ENVELOPE_BYTES + 1)
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise InvalidRequest("Fetcher envelope exceeds 256 KiB")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidRequest("Could not read fetcher envelope JSON") from exc
    envelope = _object(
        payload,
        {
            "schema_version",
            "untrusted_web_metadata",
            "bookmark",
            "retrieval",
            "warnings",
        },
        {"schema_version", "untrusted_web_metadata", "bookmark", "retrieval"},
        "Fetcher envelope",
    )
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise InvalidRequest("Fetcher envelope schema_version must be 1")
    if envelope["untrusted_web_metadata"] is not True:
        raise InvalidRequest(
            "Fetcher envelope must declare untrusted_web_metadata=true"
        )
    bookmark = _object(
        envelope["bookmark"],
        {*WEB_METADATA_FIELDS, "url", "canonical_url", "fetch_status", "bookmark_kind"},
        {"url", "canonical_url", "fetch_status"},
        "Fetcher bookmark",
    )
    retrieval = _object(
        envelope["retrieval"],
        {"requested_url", "final_url", "retrieved_at", "status", "http_status"},
        {"retrieved_at", "status"},
        "Fetcher retrieval",
    )
    warnings = envelope.get("warnings", [])
    if (
        not isinstance(warnings, list)
        or len(warnings) > 64
        or any(not isinstance(item, str) or len(item) > 2000 for item in warnings)
    ):
        raise InvalidRequest("Fetcher warnings must be a bounded array of strings")
    status = retrieval["status"]
    if status not in RETRIEVAL_STATUSES or status != bookmark["fetch_status"]:
        raise InvalidRequest("Fetcher retrieval and bookmark status must agree")
    if "http_status" in retrieval and (
        type(retrieval["http_status"]) is not int
        or not 100 <= retrieval["http_status"] <= 599
    ):
        raise InvalidRequest("Fetcher http_status must be an integer from 100 to 599")
    for field in ("url", "canonical_url"):
        _url(bookmark[field])
    for field in ("requested_url", "final_url"):
        if field in retrieval:
            _url(retrieval[field])
    if "final_url" in retrieval and _url(retrieval["final_url"]) != _url(
        bookmark["url"]
    ):
        raise InvalidRequest("Fetcher final_url must match bookmark.url")
    definition = registry.require_type("bookmark")
    try:
        definition.fields["retrieved_at"].validate_value(
            retrieval["retrieved_at"], "retrieval.retrieved_at"
        )
        for field, value in bookmark.items():
            if field == "title":
                if not isinstance(value, str) or len(value) > 500 or "\x00" in value:
                    raise InvalidRequest("Fetcher title must be a bounded string")
            else:
                definition.fields[field].validate_value(value, f"bookmark.{field}")
            strings = value if isinstance(value, list) else [value]
            limit = 500 if field == "authors" else 10000
            if any(
                isinstance(item, str) and ("\x00" in item or len(item) > limit)
                for item in strings
            ):
                raise InvalidRequest("Fetcher metadata contains an invalid string")
    except SchemaPackError as exc:
        raise InvalidRequest(str(exc)) from exc
    return bookmark, retrieval


def _missing(value: object) -> bool:
    return (
        value is None or value == [] or (isinstance(value, str) and not value.strip())
    )


def command_refresh_bookmark(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_expected_revision(path, args.expected_revision)
    if get_schema_version(metadata) < 10:
        raise UnsupportedSchema(
            "Bookmark refresh needs schema 10 field provenance; run noetrail migrate"
        )
    if metadata.get("type") != "bookmark":
        raise InvalidRequest("refresh-bookmark requires an active bookmark")
    bookmark, retrieval = load_envelope(args.metadata_file, args.registry)
    requested = _url(retrieval.get("requested_url", bookmark["url"]))
    stored_urls = {_url(metadata.get("url")), _url(metadata.get("canonical_url"))}
    if requested not in stored_urls:
        raise InvalidRequest("Fetcher request does not match the saved bookmark URL")
    changed: list[str] = []
    provenance = entry_provenance(metadata)
    for field in WEB_METADATA_FIELDS:
        # Even a failed fetcher's host fallback must never replace the user's title.
        if field == "title" or field not in bookmark:
            continue
        if _missing(metadata.get(field)) and not _missing(bookmark[field]):
            metadata[field] = bookmark[field]
            provenance[field] = "web"
            changed.append(field)
    kind = bookmark.get("bookmark_kind", "unknown")
    if metadata.get("bookmark_kind") == "unknown" and kind != "unknown":
        metadata["bookmark_kind"] = kind
        changed.append("bookmark_kind")
    for field, value in (
        ("fetch_status", retrieval["status"]),
        ("retrieved_at", retrieval["retrieved_at"]),
    ):
        if metadata.get(field) != value:
            metadata[field] = value
            changed.append(field)
    if changed:
        metadata = with_provenance(metadata, provenance)
        metadata["updated_at"] = now_iso()
        atomic_write_entry(path, metadata, body)
    print(
        json.dumps(
            {
                "updated": str(path.relative_to(root)),
                "id": args.id,
                "changed": changed,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0

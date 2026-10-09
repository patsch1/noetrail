#!/usr/bin/env python3
"""Dependency-free MCP bridge exposing narrow knowledge-vault operations.

The bridge deliberately does not expose shell, arbitrary paths, schema
migrations, duplicate overrides, full-body replacement, or permanent purge.
Every operation is expressed as a ``noetrail.cli`` argument vector, so the CLI
remains the single implementation of vault rules.

Mutations and ``validate`` run in a separate CLI process, which keeps a hard
timeout and crash isolation around them. The frequent bounded reads run in this
process, because a fresh interpreter costs more than the read itself.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Sequence
import contextlib
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time

from noetrail.attachments import detect_attachment_image, read_attachment_blob
from noetrail.cli import ERROR_FORMAT_VARIABLE, run_command
from noetrail.constants import MAX_DELIVERABLE_ATTACHMENT_BYTES
from noetrail.deadline import read_deadline
from noetrail.errors import ConfigurationError, NoetrailError
from noetrail.jsonrpc import JsonRequestError, loads_bounded
from noetrail.layout import LayoutError, NoetrailLayout, resolve_layout
from noetrail.mcp_protocol import (
    LEGACY_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSION,
    ProtocolRequestError,
    discovery_result,
    request_protocol,
    result_envelope,
)
from noetrail.schema import SchemaPackError, SchemaRegistry
from noetrail.search import RANK_BM25, RANK_HYBRID, RANK_SUBSTRING
from noetrail.version import application_version

PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSION
SERVER_VERSION = application_version()
MAX_REQUEST_CHARS = 300_000
MAX_RESULT_CHARS = 1_000_000
TOOL_TIMEOUT_SECONDS = 30
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_RECIPE_MINUTES = 7 * 24 * 60
DEFAULT_PENDING_ATTACHMENT_AGE_SECONDS = 15 * 60
MAX_PENDING_ATTACHMENT_AGE_SECONDS = 60 * 60
DEFAULT_PENDING_ATTACHMENT_LIMIT = 8
MAX_PENDING_ATTACHMENT_LIMIT = 16
MAX_PENDING_ATTACHMENT_CANDIDATES = 64
PENDING_ATTACHMENT_TOKEN_TTL_SECONDS = 15 * 60
MAX_DELIVERY_MARKER_TEMPLATE_CHARS = 256

ENTRY_TYPES = [
    "thought",
    "memory",
    "note",
    "person",
    "project",
    "media",
    "source",
    "place",
    "product",
    "recipe",
    "experience",
]
SENSITIVITIES = ["normal", "personal", "sensitive"]
PRECISIONS = ["unknown", "year", "month", "day", "datetime", "approximate"]
LIFECYCLE_STATUSES = ["active", "unreviewed", "archived"]
READING_STATUSES = ["unread", "reading", "read", "reference"]
BOOKMARK_KINDS = [
    "article",
    "video",
    "podcast",
    "tool",
    "product",
    "website",
    "other",
    "unknown",
]
PLACE_KINDS = ["bar", "restaurant", "cafe", "shop", "other"]
PRODUCT_KINDS = ["rum", "spirit", "wine", "beer", "drink", "food", "other"]
INTEREST_STATUSES = ["none", "wishlist", "planned"]
EXPERIENCE_KINDS = [
    "tasting",
    "visit",
    "dining",
    "watching",
    "listening",
    "reading",
    "cooking",
    "other",
]
FETCH_STATUSES = ["complete", "partial", "blocked", "failed", "not_attempted"]
ID_PATTERN = r"^kn_[0-9a-f]{32}$"
PREDICATE_PATTERN = r"^[a-z][a-z0-9_]*$"
REVISION_PATTERN = r"^sha256:[0-9a-f]{64}$"
ATTACHMENT_TOKEN_PATTERN = r"^kit_[0-9a-f]{32}$"
QUALIFIED_TYPE_PATTERN = (
    r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$"
)


@dataclass(frozen=True)
class DeliverableImage:
    """A tool result a channel can render, not only read.

    Every other tool answers with JSON that ends up as text. An image has to
    reach the user as an image, so this carries the bytes separately and the
    dispatcher turns it into an MCP image content block. The summary travels
    with it so a caller that cannot render still learns what it received.
    """

    media_type: str
    data: bytes
    summary: dict[str, object]


class ToolFailure(Exception):
    """A bounded, user-safe failure returned as an MCP tool error.

    ``code`` is the stable identifier an agent branches on. It comes straight
    from ``noetrail.errors`` when the failure originates in the vault, so the
    server no longer has to describe a revision conflict by handing back an
    English sentence and hoping the caller recognises it.
    """

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        super().__init__(message)
        self.code = code


def _reported_failure(stderr: str) -> tuple[str, str] | None:
    """Recover ``(message, code)`` from a child CLI that reported JSON.

    Only the last line is considered, and only when it is an object with both
    fields as strings: a Python traceback or an argparse usage message must
    fall through to the raw-text path rather than be mistaken for a report.
    """

    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        reported = json.loads(lines[-1])
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(reported, dict):
        return None
    message = reported.get("message")
    code = reported.get("code")
    if not isinstance(message, str) or not isinstance(code, str):
        return None
    return message, code


@dataclass(frozen=True)
class PendingAttachment:
    """Server-internal binding from an opaque token to one inbox image."""

    path: Path
    sha256: str
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    media_type: str
    original_name: str
    expires_monotonic: float
    expires_at: str

    @property
    def fingerprint(self) -> tuple[str, int, int, int, int, str]:
        return (
            str(self.path),
            self.device,
            self.inode,
            self.size_bytes,
            self.mtime_ns,
            self.sha256,
        )


def normalize_attachment_name(value: str, extension: str) -> str:
    name = re.sub(r"^[0-9a-fA-F]{32}_", "", Path(value).name)
    name = "".join(
        character if character >= " " and character != "\x7f" else "_"
        for character in name
    ).strip()
    return name[:255] if name else f"image.{extension}"


def argv(
    subcommand: str,
    *,
    options: Sequence[str] = (),
    positionals: Sequence[str] = (),
) -> list[str]:
    """Build a CLI argument vector that argparse cannot reinterpret.

    Every agent-controlled positional value is placed after an explicit ``--``
    separator, so a value such as ``--remove`` or ``--expected-revision`` is
    always consumed as data and never as an option flag. Options are emitted
    before the separator, which keeps them outside the agent's reach.

    Without the separator an agent could smuggle flags through a positional
    parameter: a tag named ``--remove`` turned an "add tags" call into a
    deletion, and a leading ``--`` pushed the trailing ``--expected-revision``
    into the positional list, silently disabling the optimistic-locking check.
    """
    vector = [subcommand, *options]
    if positionals:
        vector.append("--")
        vector.extend(positionals)
    return vector


def string_schema(
    *,
    max_length: int,
    enum: list[str] | None = None,
    pattern: str | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {"type": "string", "maxLength": max_length}
    if enum is not None:
        schema["enum"] = enum
    if pattern is not None:
        schema["pattern"] = pattern
    return schema


def object_schema(
    properties: dict[str, object],
    *,
    required: list[str] | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


ID_SCHEMA = string_schema(max_length=35, pattern=ID_PATTERN)
REVISION_SCHEMA = string_schema(max_length=71, pattern=REVISION_PATTERN)
ATTACHMENT_TOKEN_SCHEMA = string_schema(
    max_length=36,
    pattern=ATTACHMENT_TOKEN_PATTERN,
)
PREDICATE_SCHEMA = string_schema(max_length=80, pattern=PREDICATE_PATTERN)
# An ISO 8601 instant with an offset. A date alone is refused by the CLI
# rather than assumed to mean local midnight, because that assumption is what
# turns a correct interval into a wrong one when the vault or its owner moves.
# The pattern is what keeps the value out of argparse's option space at all:
# these reach the CLI as `--valid-from <value>`, so a value that begins with a
# dash would otherwise be rejected as a missing argument instead of reported as
# a bad timestamp.
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.,+Zz-]{1,40}$"
AS_OF_SCHEMA = string_schema(max_length=100, pattern=TIMESTAMP_PATTERN)
TAG_SCHEMA = string_schema(max_length=80)
ALIAS_SCHEMA = string_schema(max_length=200)
RELATION_SCHEMA = object_schema(
    {"predicate": PREDICATE_SCHEMA, "target": ID_SCHEMA},
    required=["predicate", "target"],
)
UNRESOLVED_RELATION_SCHEMA = object_schema(
    {
        "predicate": PREDICATE_SCHEMA,
        "reference": string_schema(max_length=500),
    },
    required=["predicate", "reference"],
)


def tool(
    name: str,
    description: str,
    properties: dict[str, object],
    *,
    required: list[str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "inputSchema": object_schema(properties, required=required),
    }


TOOLS = [
    tool(
        "merge_entries",
        "Preview a conservative merge of two explicitly selected entries. "
        "Review the complete plan before apply; echo its expected_plan digest. "
        "Conflicting metadata and fetched content are refused. Source goes to "
        "reversible trash and incoming edges follow the target. Never merge "
        "merely because a candidate suggestion matched.",
        {"source_id": ID_SCHEMA, "target_id": ID_SCHEMA,
         "apply": {"type": "boolean"}, "expected_plan": REVISION_SCHEMA},
        required=["source_id", "target_id"],
    ),
    tool(
        "find_candidates",
        "Suggest possible duplicates or related titles for one entry. "
        "Names and URLs are evidence for review, not proof of identity. No bodies.",
        {"id": ID_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
         "offset": {"type": "integer", "minimum": 0}},
        required=["id"],
    ),
    tool(
        "list_types",
        "List installed declarative schema packs and their qualified type IDs. "
        "This is read-only and does not return vault content.",
        {},
    ),
    tool(
        "describe_type",
        "Describe one installed qualified schema type, including its fields, "
        "body sections, and generated JSON Schema. This is read-only.",
        {
            "type_id": string_schema(
                max_length=127,
                pattern=QUALIFIED_TYPE_PATTERN,
            ),
        },
        required=["type_id"],
    ),
    tool(
        "inventory",
        "Return complete aggregate counts for every active vault entry. This "
        "read-only operation has no entry-result limit and returns no titles "
        "or bodies. attachment_count counts stored attachment references; "
        "entries_with_attachments counts entries containing them. "
        "relation_count is what holds now, or at as_of.",
        {"as_of": AS_OF_SCHEMA},
    ),
    tool(
        "list_views",
        "List declarative saved search views without reading vault content.",
        {},
    ),
    tool(
        "run_view",
        "Run one configured saved view. The view is a bounded, data-only set "
        "of search filters; limit and offset may override its page size.",
        {
            "name": string_schema(
                max_length=63,
                pattern=r"^[a-z][a-z0-9_]{0,62}$",
            ),
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
        },
        required=["name"],
    ),
    tool(
        "search",
        "Search active knowledge entries with structured filters. An empty "
        "query lists all matching entries. rank=hybrid (the default) returns "
        "both a literal match of the query and every entry containing at "
        "least one of its terms, ordered by BM25 relevance; rank=substring "
        "matches literally only; rank=bm25 ranks only. An empty hybrid search "
        "may return tentative word-form or title/alias typo candidates labelled "
        "match_kind=word_form or typo; "
        "verify their contents before answering. Prefer the default: "
        "a multi-word question finds nothing under substring alone. "
        "Returns one compact, stably sorted page with total, has_more, and "
        "next_offset; use get_entry only for the body or full metadata of a "
        "selected result. When a type filter returns total 0, the response "
        "adds without_type_filter with the count and types the same query "
        "matches once the type restriction is lifted: an entry may be stored "
        "under a type other than the one its name suggests, so report those "
        "instead of reporting that nothing exists. Relation filters and "
        "relation counts describe the state that holds now, or at as_of when "
        "one is given. Each result includes attachment_count. To find stored "
        "photos, use an empty query with has_attachment=true, without guessing "
        "photo words or an entry type. false selects entries without attachments.",
        {
            "query": string_schema(max_length=2_000),
            "explain": {"type": "boolean"},
            "has_attachment": {"type": "boolean"},
            "type": string_schema(
                max_length=20, enum=[*ENTRY_TYPES, "bookmark"]
            ),
            "domain": string_schema(max_length=255),
            "reading_status": string_schema(
                max_length=20, enum=READING_STATUSES
            ),
            "bookmark_kind": string_schema(
                max_length=20, enum=BOOKMARK_KINDS
            ),
            "read_after": string_schema(max_length=100),
            "read_before": string_schema(max_length=100),
            "place_kind": string_schema(max_length=20, enum=PLACE_KINDS),
            "product_kind": string_schema(max_length=20, enum=PRODUCT_KINDS),
            "experience_kind": string_schema(
                max_length=20, enum=EXPERIENCE_KINDS
            ),
            "interest_status": string_schema(
                max_length=20, enum=INTEREST_STATUSES
            ),
            "related_id": ID_SCHEMA,
            "relation_predicate": PREDICATE_SCHEMA,
            "experienced": {"type": "boolean"},
            "min_rating": {"type": "integer", "minimum": 1, "maximum": 5},
            "occurred_after": string_schema(max_length=100),
            "occurred_before": string_schema(max_length=100),
            "as_of": AS_OF_SCHEMA,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
            "sort": {
                "type": "string",
                "enum": [
                    "auto",
                    "relevance",
                    "updated_desc",
                    "updated_asc",
                    "title_asc",
                    "title_desc",
                    "occurred_desc",
                    "occurred_asc",
                ],
            },
            "rank": {
                "type": "string",
                "enum": [RANK_SUBSTRING, RANK_BM25, RANK_HYBRID],
            },
        },
        required=["query"],
    ),
    tool(
        "retrieve",
        "Search active entries and return the selected full bodies and metadata "
        "in one bounded call. Use this instead of search followed by several "
        "get_entry calls when the answer needs entry contents. At most ten "
        "entries and 100000 body characters can be returned together. "
        "query_variants adds up to three distinct wordings or translations "
        "to query, with deduplicated entries, reciprocal-rank fusion and one "
        "shared result/body budget. query_matches reports each matching variant. "
        "Supply translations yourself; no automatic translation is performed. "
        "Tentative candidates retain match_kind=word_form or typo; "
        "check that their contents actually answer the question. "
        "has_attachment optionally filters entries with or without attachments; "
        "full metadata includes attachment records.",
        {
            "query": string_schema(max_length=2_000),
            "query_variants": {
                "type": "array",
                "maxItems": 3,
                "items": string_schema(max_length=2_000),
            },
            "has_attachment": {"type": "boolean"},
            "type": string_schema(
                max_length=20, enum=[*ENTRY_TYPES, "bookmark"]
            ),
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "max_body_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100_000,
            },
            "rank": {
                "type": "string",
                "enum": [RANK_SUBSTRING, RANK_BM25, RANK_HYBRID],
            },
            "as_of": AS_OF_SCHEMA,
        },
        required=["query"],
    ),
    tool(
        "get_entry",
        "Read one selected active entry, including its body, by stable ID. "
        "Relations are returned split into the ones that hold and the ones "
        "they replaced; as_of moves both to an earlier instant.",
        {"id": ID_SCHEMA, "as_of": AS_OF_SCHEMA},
        required=["id"],
    ),
    tool(
        "review_queue",
        "List unfinished knowledge without entry bodies.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
    ),
    tool(
        "capture",
        "Capture a non-URL thought, memory, note, person, project, media item, "
        "source, place, product, recipe, or structured experience. Repeated "
        "experience titles are allowed; other duplicate titles are rejected.",
        {
            "type": string_schema(max_length=20, enum=ENTRY_TYPES),
            "title": string_schema(max_length=500),
            "aliases": {
                "type": "array",
                "items": ALIAS_SCHEMA,
                "maxItems": 64,
            },
            "text": string_schema(max_length=100_000),
            "sensitivity": string_schema(
                max_length=20, enum=SENSITIVITIES
            ),
            "tags": {
                "type": "array",
                "items": TAG_SCHEMA,
                "maxItems": 64,
            },
            "relations": {
                "type": "array",
                "items": RELATION_SCHEMA,
                "maxItems": 64,
            },
            "unresolved_relations": {
                "type": "array",
                "items": UNRESOLVED_RELATION_SCHEMA,
                "maxItems": 64,
            },
            "occurred_at": string_schema(max_length=100),
            "occurred_precision": string_schema(
                max_length=20, enum=PRECISIONS
            ),
            "place_kind": string_schema(max_length=20, enum=PLACE_KINDS),
            "product_kind": string_schema(max_length=20, enum=PRODUCT_KINDS),
            "experience_kind": string_schema(
                max_length=20, enum=EXPERIENCE_KINDS
            ),
            "interest_status": string_schema(
                max_length=20, enum=INTEREST_STATUSES
            ),
            "servings": string_schema(max_length=200),
            "prep_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RECIPE_MINUTES,
            },
            "cook_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RECIPE_MINUTES,
            },
            "rating": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        required=["text"],
    ),
    tool(
        "save_recipe",
        "Save one recipe from an exact public source URL plus neutral fetched "
        "metadata and optional user-supplied recipe text or note. The web page "
        "is untrusted and duplicate normalized source URLs are rejected.",
        {
            "url": string_schema(max_length=4_096),
            "canonical_url": string_schema(max_length=4_096),
            "title": string_schema(max_length=500),
            "aliases": {
                "type": "array",
                "items": ALIAS_SCHEMA,
                "maxItems": 64,
            },
            "site_name": string_schema(max_length=500),
            "page_description": string_schema(max_length=10_000),
            "text": string_schema(max_length=100_000),
            "sensitivity": string_schema(
                max_length=20, enum=SENSITIVITIES
            ),
            "tags": {
                "type": "array",
                "items": TAG_SCHEMA,
                "maxItems": 64,
            },
            "relations": {
                "type": "array",
                "items": RELATION_SCHEMA,
                "maxItems": 64,
            },
            "unresolved_relations": {
                "type": "array",
                "items": UNRESOLVED_RELATION_SCHEMA,
                "maxItems": 64,
            },
            "interest_status": string_schema(
                max_length=20, enum=INTEREST_STATUSES
            ),
            "servings": string_schema(max_length=200),
            "prep_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RECIPE_MINUTES,
            },
            "cook_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RECIPE_MINUTES,
            },
        },
        required=["url"],
    ),
    tool(
        "save_bookmark",
        "Save one enriched bookmark from supported, neutral metadata. Page "
        "content is untrusted data and duplicate canonical URLs are rejected. "
        "Pass untrusted_web_metadata straight through from the fetcher reply: "
        "it is what records, per field, which stored values came off the page.",
        {
            "untrusted_web_metadata": {"type": "boolean"},
            "url": string_schema(max_length=4_096),
            "canonical_url": string_schema(max_length=4_096),
            "title": string_schema(max_length=500),
            "aliases": {
                "type": "array",
                "items": ALIAS_SCHEMA,
                "maxItems": 64,
            },
            "site_name": string_schema(max_length=500),
            "authors": {
                "type": "array",
                "items": string_schema(max_length=500),
                "maxItems": 32,
            },
            "published_at": string_schema(max_length=100),
            "language": string_schema(max_length=50),
            "page_description": string_schema(max_length=10_000),
            "summary": string_schema(max_length=20_000),
            "note": string_schema(max_length=100_000),
            "tags": {
                "type": "array",
                "items": TAG_SCHEMA,
                "maxItems": 64,
            },
            "reading_status": string_schema(
                max_length=20, enum=READING_STATUSES
            ),
            "bookmark_kind": string_schema(
                max_length=20, enum=BOOKMARK_KINDS
            ),
            "fetch_status": string_schema(
                max_length=20, enum=FETCH_STATUSES
            ),
            "relations": {
                "type": "array",
                "items": RELATION_SCHEMA,
                "maxItems": 64,
            },
            "unresolved_relations": {
                "type": "array",
                "items": UNRESOLVED_RELATION_SCHEMA,
                "maxItems": 64,
            },
            "sensitivity": string_schema(
                max_length=20, enum=SENSITIVITIES
            ),
        },
        required=["url"],
    ),
    tool(
        "refresh_bookmark",
        "Fill missing metadata of an existing bookmark from the complete isolated "
        "fetcher envelope, preserving title, existing site name, tags and body. "
        "New page fields retain web provenance; only unknown bookmark_kind is "
        "classified. Retrieval status and timestamp are copied even after failure. "
        "Requires the latest revision and a matching requested URL; does no fetching.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "envelope": object_schema({
                "schema_version": {"type": "integer", "minimum": 1, "maximum": 1},
                "untrusted_web_metadata": {"type": "boolean"},
                "bookmark": object_schema({
                    "url": string_schema(max_length=4096),
                    "canonical_url": string_schema(max_length=4096),
                    "title": string_schema(max_length=500),
                    "site_name": string_schema(max_length=500),
                    "published_at": string_schema(max_length=100),
                    "language": string_schema(max_length=50),
                    "page_description": string_schema(max_length=10000),
                    "authors": {"type": "array", "maxItems": 32,
                                "items": string_schema(max_length=500)},
                    "bookmark_kind": string_schema(max_length=20, enum=BOOKMARK_KINDS),
                    "fetch_status": string_schema(
                        max_length=20, enum=FETCH_STATUSES[:4]
                    ),
                }, required=["url", "canonical_url", "fetch_status"]),
                "retrieval": object_schema({
                    "requested_url": string_schema(max_length=4096),
                    "final_url": string_schema(max_length=4096),
                    "retrieved_at": string_schema(max_length=100),
                    "status": string_schema(max_length=20, enum=FETCH_STATUSES[:4]),
                    "http_status": {"type": "integer", "minimum": 100, "maximum": 599},
                }, required=["retrieved_at", "status"]),
                "warnings": {"type": "array", "maxItems": 64,
                             "items": string_schema(max_length=2000)},
            }, required=[
                "schema_version", "untrusted_web_metadata", "bookmark", "retrieval"
            ]),
        },
        required=["id", "expected_revision", "envelope"],
    ),
    tool(
        "update",
        "Append user-supplied content; change title, sensitivity, bookmark "
        "kind, or structured-experience metadata; or record a legacy "
        "place/product experience. Full-body "
        "replacement is intentionally unavailable. Requires the revision from "
        "the latest read and rejects stale changes.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "title": string_schema(max_length=500),
            "aliases": {
                "type": "array",
                "items": ALIAS_SCHEMA,
                "maxItems": 64,
            },
            "append": string_schema(max_length=100_000),
            "sensitivity": string_schema(
                max_length=20, enum=SENSITIVITIES
            ),
            "bookmark_kind": string_schema(
                max_length=20, enum=BOOKMARK_KINDS
            ),
            "record_experience": {"type": "boolean"},
            "experienced_at": string_schema(max_length=100),
            "review": string_schema(max_length=100_000),
            "rating": {"type": "integer", "minimum": 1, "maximum": 5},
            "experience_kind": string_schema(
                max_length=20, enum=EXPERIENCE_KINDS
            ),
            "occurred_at": string_schema(max_length=100),
            "occurred_precision": string_schema(
                max_length=20, enum=PRECISIONS
            ),
            "source_url": string_schema(max_length=4_096),
            "source_site_name": string_schema(max_length=500),
            "source_description": string_schema(max_length=10_000),
            "servings": string_schema(max_length=200),
            "prep_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RECIPE_MINUTES,
            },
            "cook_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RECIPE_MINUTES,
            },
        },
        required=["id", "expected_revision"],
    ),
    tool(
        "get_attachment",
        "Return one image already attached to an entry, as image content the "
        "channel can display. Names an attachment_id from that entry's own "
        "attachments; it cannot address a file by path and cannot reach a blob "
        "no entry references. Use it when the user asks to see a photo that is "
        "stored. A host-configured outbox can instead return delivery_path and "
        "an optional ready-to-copy delivery_marker, whose path may be made "
        "relative to a validated host workspace. Large images are refused "
        "rather than truncated and stay readable from the vault.",
        {
            "id": ID_SCHEMA,
            "attachment_id": {
                "type": "string",
                "pattern": r"^ka_[0-9a-f]{32}$",
            },
        },
        required=["id", "attachment_id"],
    ),
    tool(
        "list_pending_attachments",
        "List supported images received recently in the server's fixed "
        "attachment inbox. Returns short-lived opaque tokens, never source "
        "paths. Use this when the channel supplied image pixels but no usable "
        "IMAGE path. Results are ordered oldest first. Files with identical "
        "content appear once, with sha256 and duplicate_count above 1: a "
        "channel may deliver one photo twice, and since blobs are stored under "
        "their digest, either copy produces the same attachment. Treat that as "
        "one image, not as an ambiguous choice.",
        {
            "max_age_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PENDING_ATTACHMENT_AGE_SECONDS,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PENDING_ATTACHMENT_LIMIT,
            },
        },
    ),
    tool(
        "add_attachment",
        "Copy one supported image from the server's fixed attachment inbox "
        "into an existing vault entry. Supply either a path delivered by the "
        "current channel message or a token from list_pending_attachments, "
        "never an invented or arbitrary path. "
        "Requires the revision from the latest read and rejects stale changes.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "source_path": string_schema(max_length=4_096),
            "attachment_token": ATTACHMENT_TOKEN_SCHEMA,
            "caption": string_schema(max_length=2_000),
            "experienced_at": string_schema(max_length=100),
        },
        required=["id", "expected_revision"],
    ),
    tool(
        "set_relation",
        "Add or remove a typed relation between two existing entries, "
        "optionally resolving a matching pending reference. When adding, "
        "valid_from and valid_until record when the assertion holds in the "
        "world, and supersedes names the target of the same-predicate "
        "relation this one replaces from valid_from on. The replaced relation "
        "stays stored and readable; it is closed, not deleted.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "predicate": PREDICATE_SCHEMA,
            "target_id": ID_SCHEMA,
            "action": string_schema(
                max_length=10, enum=["add", "remove"]
            ),
            "resolve_reference": string_schema(max_length=500),
            "valid_from": AS_OF_SCHEMA,
            "valid_until": AS_OF_SCHEMA,
            "supersedes": ID_SCHEMA,
        },
        required=[
            "id",
            "expected_revision",
            "predicate",
            "target_id",
            "action",
        ],
    ),
    tool(
        "set_unresolved_relation",
        "Add or remove a grounded free-text relation reference for later review.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "predicate": PREDICATE_SCHEMA,
            "reference": string_schema(max_length=500),
            "action": string_schema(
                max_length=10, enum=["add", "remove"]
            ),
        },
        required=[
            "id",
            "expected_revision",
            "predicate",
            "reference",
            "action",
        ],
    ),
    tool(
        "set_tags",
        "Add or remove tags on one active entry.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "tags": {
                "type": "array",
                "items": TAG_SCHEMA,
                "minItems": 1,
                "maxItems": 64,
            },
            "action": string_schema(
                max_length=10, enum=["add", "remove"]
            ),
        },
        required=["id", "expected_revision", "tags", "action"],
    ),
    tool(
        "set_status",
        "Change an entry lifecycle status, bookmark reading status, and/or "
        "place/product/recipe interest status.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "lifecycle": string_schema(
                max_length=20, enum=LIFECYCLE_STATUSES
            ),
            "reading": string_schema(
                max_length=20, enum=READING_STATUSES
            ),
            "interest": string_schema(
                max_length=20, enum=INTEREST_STATUSES
            ),
        },
        required=["id", "expected_revision"],
    ),
    tool(
        "complete_review",
        "Mark one resolved review item as reviewed. Unresolved relations still "
        "block completion.",
        {"id": ID_SCHEMA, "expected_revision": REVISION_SCHEMA},
        required=["id", "expected_revision"],
    ),
    tool(
        "relations",
        "Show outgoing and derived incoming relations for one stable ID, "
        "split into the ones in force and the ones they superseded.",
        {"id": ID_SCHEMA, "as_of": AS_OF_SCHEMA},
        required=["id"],
    ),
    tool(
        "list_trash",
        "List trashed entries without their bodies.",
        {},
    ),
    tool(
        "trash",
        "Move exactly one active entry to the reversible 90-day trash.",
        {
            "id": ID_SCHEMA,
            "expected_revision": REVISION_SCHEMA,
            "reason": string_schema(max_length=2_000),
        },
        required=["id", "expected_revision"],
    ),
    tool(
        "restore",
        "Restore exactly one trashed entry to its original path without overwriting.",
        {"id": ID_SCHEMA, "expected_revision": REVISION_SCHEMA},
        required=["id", "expected_revision"],
    ),
    tool(
        "validate",
        "Validate active and trashed entries against the current vault rules.",
        {},
    ),
]

def build_tools(registry: SchemaRegistry) -> list[dict[str, object]]:
    """Derive the process-fixed MCP surface from the loaded registry."""

    tools = copy.deepcopy(TOOLS)
    custom_types = registry.custom_types
    if not custom_types:
        return tools
    by_name = {
        item["name"]: item
        for item in tools
        if isinstance(item.get("name"), str)
    }
    qualified_ids = sorted(custom_types)

    capture_schema = by_name["capture"]["inputSchema"]
    assert isinstance(capture_schema, dict)
    capture_properties = capture_schema["properties"]
    assert isinstance(capture_properties, dict)
    capture_properties["type"] = string_schema(
        max_length=127,
        enum=[*ENTRY_TYPES, *qualified_ids],
    )
    capture_properties["attributes"] = {
        "anyOf": [
            custom_types[type_id].attributes_json_schema()
            for type_id in qualified_ids
        ]
    }
    capture_schema.pop("required", None)
    capture_conditions: list[dict[str, object]] = [
        {
            "if": {
                "not": {
                    "properties": {
                        "type": {"enum": qualified_ids}
                    },
                    "required": ["type"],
                }
            },
            "then": {"required": ["text"]},
        }
    ]
    for type_id in qualified_ids:
        type_definition = custom_types[type_id]
        then: dict[str, object] = {
            "properties": {
                "attributes": type_definition.attributes_json_schema()
            }
        }
        if any(
            field.required and not field.has_default
            for field in type_definition.fields.values()
        ):
            then["required"] = ["attributes"]
        capture_conditions.append(
            {
                "if": {
                    "properties": {"type": {"const": type_id}},
                    "required": ["type"],
                },
                "then": then,
            }
        )
    capture_schema["allOf"] = capture_conditions

    update_schema = by_name["update"]["inputSchema"]
    assert isinstance(update_schema, dict)
    update_properties = update_schema["properties"]
    assert isinstance(update_properties, dict)
    update_properties["attributes"] = {
        "anyOf": [
            custom_types[type_id].attributes_patch_json_schema()
            for type_id in qualified_ids
        ]
    }

    search_schema = by_name["search"]["inputSchema"]
    assert isinstance(search_schema, dict)
    search_properties = search_schema["properties"]
    assert isinstance(search_properties, dict)
    search_properties["type"] = string_schema(
        max_length=127,
        enum=[*ENTRY_TYPES, "bookmark", *qualified_ids],
    )
    retrieve_schema = by_name["retrieve"]["inputSchema"]
    assert isinstance(retrieve_schema, dict)
    retrieve_properties = retrieve_schema["properties"]
    assert isinstance(retrieve_properties, dict)
    retrieve_properties["type"] = string_schema(
        max_length=127,
        enum=[*ENTRY_TYPES, "bookmark", *qualified_ids],
    )
    searchable_types = [
        type_id
        for type_id in qualified_ids
        if any(
            field.searchable
            for field in custom_types[type_id].fields.values()
        )
    ]
    if searchable_types:
        search_properties["attribute_filters"] = {
            "anyOf": [
                custom_types[type_id].search_filters_json_schema()
                for type_id in searchable_types
            ]
        }
        search_schema["dependentRequired"] = {
            "attribute_filters": ["type"]
        }
        search_schema["allOf"] = [
            {
                "if": {
                    "properties": {"type": {"const": type_id}},
                    "required": ["type"],
                },
                "then": {
                    "properties": {
                        "attribute_filters": custom_types[
                            type_id
                        ].search_filters_json_schema()
                    }
                },
            }
            for type_id in searchable_types
        ]

    return tools


def validate_value(value: object, schema: dict[str, object], path: str = "$") -> None:
    """Validate the JSON-schema subset used by this server."""

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolFailure(f"{path} must be an object")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise RuntimeError("Invalid server schema")
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise RuntimeError("Invalid server schema")
        missing = [name for name in required if name not in value]
        if missing:
            raise ToolFailure(f"{path} is missing: {', '.join(missing)}")
        unknown = sorted(set(value) - set(properties))
        if schema.get("additionalProperties") is False and unknown:
            raise ToolFailure(f"{path} has unknown fields: {', '.join(unknown)}")
        for name, child in value.items():
            child_schema = properties.get(name)
            if not isinstance(child_schema, dict):
                continue
            validate_value(child, child_schema, f"{path}.{name}")
        return

    if expected == "array":
        if not isinstance(value, list):
            raise ToolFailure(f"{path} must be an array")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ToolFailure(f"{path} must contain at least {minimum} item(s)")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ToolFailure(f"{path} must contain at most {maximum} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                validate_value(child, item_schema, f"{path}[{index}]")
        return

    if expected == "string":
        if not isinstance(value, str):
            raise ToolFailure(f"{path} must be a string")
        if "\x00" in value:
            raise ToolFailure(f"{path} must not contain NUL bytes")
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ToolFailure(f"{path} is longer than {maximum} characters")
        allowed = schema.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            raise ToolFailure(f"{path} has an unsupported value")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            raise ToolFailure(f"{path} has an invalid format")
        return

    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolFailure(f"{path} must be an integer")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            raise ToolFailure(f"{path} must be at least {minimum}")
        if isinstance(maximum, int) and value > maximum:
            raise ToolFailure(f"{path} must be at most {maximum}")
        return

    if expected == "boolean" and not isinstance(value, bool):
        raise ToolFailure(f"{path} must be a boolean")


class NoetrailServer:
    def __init__(
        self,
        layout: NoetrailLayout,
        know_script: Path | None = None,
        attachment_inbox: Path | None = None,
        attachment_outbox: Path | None = None,
        in_process_reads: bool = True,
        *,
        attachment_delivery_marker_template: str | None = None,
        attachment_delivery_marker_root: Path | None = None,
    ) -> None:
        self.layout = layout
        self.in_process_reads = in_process_reads
        self.root = layout.data_root
        try:
            self.schema_registry = SchemaRegistry.load(layout)
        except SchemaPackError as exc:
            raise ConfigurationError(f"Invalid schema pack: {exc}") from exc
        self.tools = build_tools(self.schema_registry)
        self.tool_schemas: dict[str, dict[str, object]] = {}
        for item in self.tools:
            tool_name = item.get("name")
            input_schema = item.get("inputSchema")
            if isinstance(tool_name, str) and isinstance(input_schema, dict):
                self.tool_schemas[tool_name] = input_schema
        self.know_script = know_script.resolve() if know_script else None
        self.attachment_inbox = (
            attachment_inbox.expanduser().resolve()
            if attachment_inbox is not None
            else None
        )
        # Where a stored image is placed so the host can deliver it. The host
        # decides what it can send from: this deployment's Matrix channel
        # uploads a file only when the reply names a path inside the agent's
        # own workspace, so the vault -- which is not in that workspace -- has
        # to hand a copy across. Unset, `get_attachment` still returns the
        # image itself and a client that renders MCP image content needs
        # nothing else.
        self.attachment_outbox = (
            attachment_outbox.expanduser().resolve()
            if attachment_outbox is not None
            else None
        )
        self.attachment_delivery_marker_template = (
            self._validate_delivery_marker_template(
                attachment_delivery_marker_template
            )
            if attachment_delivery_marker_template is not None
            else None
        )
        self.attachment_delivery_marker_root = (
            attachment_delivery_marker_root.expanduser().resolve()
            if attachment_delivery_marker_root is not None
            else None
        )
        if (
            self.attachment_delivery_marker_template is not None
            and self.attachment_outbox is None
        ):
            raise ConfigurationError(
                "Attachment delivery marker template requires "
                "--attachment-outbox"
            )
        if (
            self.attachment_delivery_marker_root is not None
            and self.attachment_delivery_marker_template is None
        ):
            raise ConfigurationError(
                "Attachment delivery marker root requires "
                "--attachment-delivery-marker-template"
            )
        if (
            self.attachment_delivery_marker_root is not None
            and self.attachment_outbox is not None
            and not self.attachment_outbox.is_relative_to(
                self.attachment_delivery_marker_root
            )
        ):
            raise ConfigurationError(
                "Attachment outbox must be inside the attachment delivery "
                "marker root"
            )
        self.pending_attachments: dict[str, PendingAttachment] = {}
        self.pending_tokens_by_fingerprint: dict[
            tuple[str, int, int, int, int, str], str
        ] = {}
        self.layout.validate_paths()
        if self.know_script is not None and not self.know_script.is_file():
            raise ConfigurationError(
                f"CLI script does not exist: {self.know_script}"
            )

    @staticmethod
    def _validate_delivery_marker_template(template: str) -> str:
        """Validate one operator-controlled, host-specific path wrapper."""

        if not template or len(template) > MAX_DELIVERY_MARKER_TEMPLATE_CHARS:
            raise ConfigurationError(
                "Attachment delivery marker template must contain 1 to "
                f"{MAX_DELIVERY_MARKER_TEMPLATE_CHARS} characters"
            )
        if template.count("{path}") != 1:
            raise ConfigurationError(
                "Attachment delivery marker template must contain {path} exactly once"
            )
        remainder = template.replace("{path}", "")
        if "{" in remainder or "}" in remainder:
            raise ConfigurationError(
                "Attachment delivery marker template contains an unknown placeholder"
            )
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in template
        ):
            raise ConfigurationError(
                "Attachment delivery marker template must be one line without "
                "control characters"
            )
        return template

    def _delivery_marker(self, delivery_path: str) -> str | None:
        """Wrap a prepared path in syntax selected by the embedding host."""

        if self.attachment_delivery_marker_template is None:
            return None
        marker_path = Path(delivery_path)
        if self.attachment_delivery_marker_root is not None:
            try:
                marker_path = marker_path.relative_to(
                    self.attachment_delivery_marker_root
                )
            except ValueError as exc:
                raise ToolFailure(
                    "Prepared attachment is outside the delivery marker root"
                ) from exc
        return self.attachment_delivery_marker_template.replace(
            "{path}", marker_path.as_posix()
        )

    def _attachment_inbox_directory(self) -> Path:
        if self.attachment_inbox is None:
            raise ToolFailure(
                "Attachment inbox is not configured; start the tool with "
                "--attachment-inbox"
            )
        try:
            inbox = self.attachment_inbox.resolve(strict=True)
        except OSError as exc:
            raise ToolFailure("Attachment inbox is unavailable") from exc
        if not inbox.is_dir():
            raise ToolFailure("Attachment inbox is not a directory")
        return inbox

    @staticmethod
    def _read_pending_attachment(
        candidate: Path,
        inbox: Path,
    ) -> tuple[bytes, os.stat_result] | None:
        if candidate.parent != inbox:
            return None
        try:
            before = candidate.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return None
        if before.st_size < 1 or before.st_size > MAX_ATTACHMENT_BYTES:
            return None

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(candidate, flags)
        except OSError:
            return None
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                return None
            if (details.st_dev, details.st_ino) != (before.st_dev, before.st_ino):
                return None
            if details.st_size < 1 or details.st_size > MAX_ATTACHMENT_BYTES:
                return None
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(MAX_ATTACHMENT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(data) < 1 or len(data) > MAX_ATTACHMENT_BYTES:
            return None
        return data, details

    def _expire_pending_attachments(self, now_monotonic: float) -> None:
        expired = [
            token
            for token, pending in self.pending_attachments.items()
            if pending.expires_monotonic <= now_monotonic
        ]
        for token in expired:
            self._consume_pending_attachment(token)

    def _consume_pending_attachment(self, token: str) -> None:
        pending = self.pending_attachments.pop(token, None)
        if pending is None:
            return
        if self.pending_tokens_by_fingerprint.get(pending.fingerprint) == token:
            del self.pending_tokens_by_fingerprint[pending.fingerprint]

    def _pending_token(
        self,
        *,
        candidate: Path,
        data: bytes,
        details: os.stat_result,
        media_type: str,
        extension: str,
        now_monotonic: float,
        now_epoch: float,
    ) -> tuple[str, PendingAttachment]:
        digest = hashlib.sha256(data).hexdigest()
        fingerprint = (
            str(candidate),
            details.st_dev,
            details.st_ino,
            len(data),
            details.st_mtime_ns,
            digest,
        )
        existing_token = self.pending_tokens_by_fingerprint.get(fingerprint)
        if existing_token is not None:
            existing = self.pending_attachments.get(existing_token)
            if existing is not None:
                return existing_token, existing

        token = f"kit_{secrets.token_hex(16)}"
        pending = PendingAttachment(
            path=candidate,
            sha256=digest,
            size_bytes=len(data),
            mtime_ns=details.st_mtime_ns,
            device=details.st_dev,
            inode=details.st_ino,
            media_type=media_type,
            original_name=normalize_attachment_name(candidate.name, extension),
            expires_monotonic=(
                now_monotonic + PENDING_ATTACHMENT_TOKEN_TTL_SECONDS
            ),
            expires_at=datetime.fromtimestamp(
                now_epoch + PENDING_ATTACHMENT_TOKEN_TTL_SECONDS,
                UTC,
            ).isoformat(),
        )
        self.pending_attachments[token] = pending
        self.pending_tokens_by_fingerprint[pending.fingerprint] = token
        return token, pending

    def list_pending_attachments(
        self,
        *,
        max_age_seconds: int,
        limit: int,
    ) -> dict[str, object]:
        inbox = self._attachment_inbox_directory()
        now_epoch = time.time()
        now_monotonic = time.monotonic()
        self._expire_pending_attachments(now_monotonic)

        candidates: list[tuple[int, str, Path]] = []
        try:
            children = inbox.iterdir()
            for candidate in children:
                try:
                    details = candidate.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(
                    details.st_mode
                ):
                    continue
                if details.st_size < 1 or details.st_size > MAX_ATTACHMENT_BYTES:
                    continue
                age_seconds = max(0.0, now_epoch - details.st_mtime)
                if age_seconds > max_age_seconds:
                    continue
                candidates.append(
                    (details.st_mtime_ns, candidate.name, candidate)
                )
        except OSError as exc:
            raise ToolFailure("Attachment inbox could not be read") from exc

        candidates.sort(reverse=True)
        candidates = candidates[:MAX_PENDING_ATTACHMENT_CANDIDATES]
        candidates.sort()
        result: list[dict[str, object]] = []
        # A channel can deliver one photo twice, and it did: two files, one
        # image, differing only in the inbox's own name prefix. Listed
        # separately they look like two photos for one message, which is the
        # ambiguity the caller is told to refuse -- so a duplicate delivery
        # cost the attachment entirely.
        #
        # There is nothing to choose between them. Blobs are content-addressed,
        # so identical bytes reach the same path under the same digest whichever
        # token is used. Collapsing them removes a decision that never had two
        # outcomes, and `duplicate_count` keeps the fact visible.
        by_digest: dict[str, int] = {}
        copies: dict[str, int] = {}
        for _, _, candidate in candidates:
            inspected = self._read_pending_attachment(candidate, inbox)
            if inspected is None:
                continue
            data, details = inspected
            detected = detect_attachment_image(data)
            if detected is None:
                continue
            media_type, extension = detected
            token, pending = self._pending_token(
                candidate=candidate,
                data=data,
                details=details,
                media_type=media_type,
                extension=extension,
                now_monotonic=now_monotonic,
                now_epoch=now_epoch,
            )
            position = by_digest.get(pending.sha256)
            if position is not None:
                copies[pending.sha256] += 1
                result[position]["duplicate_count"] = copies[pending.sha256]
                continue
            by_digest[pending.sha256] = len(result)
            copies[pending.sha256] = 1
            result.append(
                {
                    "attachment_token": token,
                    "received_at": datetime.fromtimestamp(
                        details.st_mtime,
                        UTC,
                    ).isoformat(),
                    "expires_at": pending.expires_at,
                    "size_bytes": pending.size_bytes,
                    "media_type": pending.media_type,
                    "original_name": pending.original_name,
                    "sha256": pending.sha256,
                    "duplicate_count": 1,
                }
            )
            if len(result) >= limit:
                break
        return {
            "attachments": result,
            "count": len(result),
            "order": "oldest_first",
            "max_age_seconds": max_age_seconds,
            "token_ttl_seconds": PENDING_ATTACHMENT_TOKEN_TTL_SECONDS,
        }

    def _place_for_delivery(
        self,
        attachment: dict[str, object],
        data: bytes,
    ) -> str | None:
        """Copy the image where the host can send it from, and say where.

        Returning image content is the portable answer, and a client that
        renders it needs nothing else. This deployment's channel is not such a
        client: it uploads a file only when the reply names a path inside the
        workspace it sends from, and the vault is not in that workspace. So the
        bytes are placed in one configured directory and the path is reported.

        The name is the content digest, so asking twice writes the same file
        rather than a second copy, and no personal filename is spread into a
        directory the vault does not own.
        """

        if self.attachment_outbox is None:
            return None
        digest = attachment.get("sha256")
        source = attachment.get("path")
        if not isinstance(digest, str) or not isinstance(source, str):
            return None
        try:
            self.attachment_outbox.mkdir(parents=True, exist_ok=True)
            destination = self.attachment_outbox / f"{digest}{Path(source).suffix}"
            if not destination.exists():
                # Written beside the target and renamed, so a reader never sees
                # a half-written image under a name that promises the digest.
                temporary = destination.with_name(f".{destination.name}.partial")
                temporary.write_bytes(data)
                temporary.replace(destination)
        except OSError as exc:
            raise ToolFailure(
                f"Attachment could not be prepared for delivery: {exc}"
            ) from exc
        return str(destination)

    def _entry_attachment(
        self,
        entry: object,
        attachment_id: str,
    ) -> dict[str, object]:
        """Find one attachment record inside an entry read, or refuse.

        Refusing here rather than falling through to the filesystem is what
        keeps the tool from being a file reader with extra steps.
        """

        records: list[object] = []
        if isinstance(entry, dict):
            found = entry.get("attachments")
            if isinstance(found, list):
                records = found
        for record in records:
            if (
                isinstance(record, dict)
                and record.get("id") == attachment_id
            ):
                return record
        raise ToolFailure(
            f"Entry has no attachment {attachment_id}; read the entry to see "
            "which attachments it holds"
        )

    def _resolve_pending_attachment(self, token: str) -> PendingAttachment:
        now_monotonic = time.monotonic()
        self._expire_pending_attachments(now_monotonic)
        pending = self.pending_attachments.get(token)
        if pending is None:
            raise ToolFailure(
                "Attachment token is unknown, expired, or already used; "
                "list pending attachments again"
            )
        return pending

    def _cli_arguments(self, arguments: list[str]) -> list[str]:
        command = list(self.layout.cli_root_arguments())
        if self.attachment_inbox is not None:
            command.extend(["--attachment-inbox", str(self.attachment_inbox)])
        command.extend(arguments)
        return command

    @staticmethod
    def _result(stdout: str) -> object:
        if len(stdout) > MAX_RESULT_CHARS:
            raise ToolFailure("Knowledge result is too large; narrow the request")
        if not stdout:
            return {"ok": True}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"message": stdout}

    def _run_read(self, arguments: list[str]) -> object:
        """Run a bounded read either in this process or in a fresh CLI process.

        Both paths execute the same CLI code under the same shared vault lock;
        only the process boundary differs. An explicit CLI script remains a
        process-wide implementation override, so it must handle reads too.
        """

        if self.in_process_reads and self.know_script is None:
            return self._run_in_process(arguments)
        return self._run(arguments)

    def _run_in_process(self, arguments: list[str]) -> object:
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        # `run_command` returns an int; `argparse` may still raise SystemExit
        # for a usage error, and `SystemExit.code` is then a string or None.
        code: int | str | None
        try:
            with (
                read_deadline(TOOL_TIMEOUT_SECONDS),
                contextlib.redirect_stdout(captured_stdout),
                contextlib.redirect_stderr(captured_stderr),
            ):
                code = run_command(self._cli_arguments(arguments))
        except NoetrailError as exc:
            # The failure arrives typed, so the code travels with it instead
            # of being reconstructed from the sentence downstream.
            raise ToolFailure(exc.message[:8_000], code=exc.code) from exc
        except SystemExit as exc:
            code = exc.code
            if code is None or (isinstance(code, int) and code == 0):
                return self._result(captured_stdout.getvalue().strip())
            detail = (
                (code if isinstance(code, str) else "")
                or captured_stderr.getvalue().strip()
                or captured_stdout.getvalue().strip()
                or f"CLI exited with {code}"
            )
            raise ToolFailure(detail[:8_000]) from exc
        except ToolFailure:
            raise
        # A defect in the CLI must not end the server loop, so the in-process
        # path absorbs what a separate process would have absorbed by exiting.
        except Exception as exc:
            raise ToolFailure(f"Knowledge operation failed: {exc}") from exc

        stdout = captured_stdout.getvalue().strip()
        if code:
            detail = (
                captured_stderr.getvalue().strip()
                or stdout
                or f"CLI exited with {code}"
            )
            raise ToolFailure(detail[:8_000])
        return self._result(stdout)

    def _run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
    ) -> object:
        if self.know_script is not None:
            invocation = [str(self.know_script)]
        else:
            invocation = ["-m", "noetrail.cli"]
        command = [
            sys.executable,
            *invocation,
            *self._cli_arguments(arguments),
        ]
        clean_environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            # The child runs without the parent environment, so the package
            # location has to be restated explicitly.
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            # Ask the child to report a refusal as one JSON object so the
            # error code survives the process boundary that mutating commands
            # still cross. Reads take the in-process path and keep the
            # exception itself.
            ERROR_FORMAT_VARIABLE: "json",
        }
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=clean_environment,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=TOOL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolFailure("Knowledge operation timed out") from exc
        except OSError as exc:
            raise ToolFailure(f"Could not start knowledge CLI: {exc}") from exc

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            reported = _reported_failure(stderr)
            if reported is not None:
                message, code = reported
                raise ToolFailure(message[:8_000], code=code)
            detail = (
                stderr or stdout or f"CLI exited with {completed.returncode}"
            )
            raise ToolFailure(detail[:8_000])
        return self._result(stdout)

    def _run_with_payload(
        self,
        command: str,
        option: str,
        payload: dict[str, object],
        *,
        prefix_arguments: list[str] | None = None,
        positionals: Sequence[str] = (),
        input_text: str | None = None,
    ) -> object:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="knowledge-mcp-",
                suffix=".json",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                path = Path(handle.name)
            arguments = argv(
                command,
                options=[*(prefix_arguments or []), option, str(path)],
                positionals=positionals,
            )
            return self._run(arguments, input_text=input_text)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    @staticmethod
    def _relation_argument(relation: dict[str, object]) -> str:
        return f"{relation['predicate']}:{relation['target']}"

    @staticmethod
    def _unresolved_argument(relation: dict[str, object]) -> str:
        return f"{relation['predicate']}:{relation['reference']}"

    def call_tool(self, name: str, arguments: object) -> object:
        schema = self.tool_schemas.get(name)
        if schema is None:
            raise ToolFailure(f"Unknown knowledge tool: {name}")
        validate_value(arguments, schema)
        assert isinstance(arguments, dict)

        if name == "list_types":
            return self.schema_registry.summary()

        if name == "describe_type":
            try:
                type_definition = self.schema_registry.require_type(
                    str(arguments["type_id"])
                )
            except SchemaPackError as exc:
                raise ToolFailure(str(exc)) from exc
            return type_definition.as_dict(include_schema=True)

        if name == "merge_entries":
            merge_command = []
            if arguments.get("apply"):
                merge_command.append("--apply")
            if "expected_plan" in arguments:
                merge_command.extend(
                    ["--expected-plan", str(arguments["expected_plan"])]
                )
            return self._run(argv(
                "merge", options=merge_command,
                positionals=[str(arguments["source_id"]), str(arguments["target_id"])],
            ))

        if name == "find_candidates":
            return self._run_read(argv(
                "candidates",
                options=["--limit", str(arguments.get("limit", 20)),
                         "--offset", str(arguments.get("offset", 0))],
                positionals=[str(arguments["id"])],
            ))

        if name == "inventory":
            command: list[str] = []
            if "as_of" in arguments:
                command.extend(["--as-of", str(arguments["as_of"])])
            return self._run_read(argv("inventory", options=command))

        if name == "list_views":
            return self._run_read(["view", "list"])

        if name == "run_view":
            command = []
            if "limit" in arguments:
                command.extend(["--limit", str(arguments["limit"])])
            if "offset" in arguments:
                command.extend(["--offset", str(arguments["offset"])])
            return self._run_read(
                argv(
                    "view",
                    options=["run", *command],
                    positionals=[str(arguments["name"])],
                )
            )

        if name == "search":
            command = []
            if "has_attachment" in arguments:
                command.append(
                    "--has-attachment" if arguments["has_attachment"]
                    else "--no-has-attachment"
                )
            if "type" in arguments:
                command.extend(["--type", str(arguments["type"])])
            if "domain" in arguments:
                command.extend(["--domain", str(arguments["domain"])])
            if "reading_status" in arguments:
                command.extend(
                    ["--reading-status", str(arguments["reading_status"])]
                )
            if "bookmark_kind" in arguments:
                command.extend(
                    ["--bookmark-kind", str(arguments["bookmark_kind"])]
                )
            if "read_after" in arguments:
                command.extend(["--read-after", str(arguments["read_after"])])
            if "read_before" in arguments:
                command.extend(["--read-before", str(arguments["read_before"])])
            if "place_kind" in arguments:
                command.extend(["--place-kind", str(arguments["place_kind"])])
            if "product_kind" in arguments:
                command.extend(
                    ["--product-kind", str(arguments["product_kind"])]
                )
            if "experience_kind" in arguments:
                command.extend(
                    ["--experience-kind", str(arguments["experience_kind"])]
                )
            if "interest_status" in arguments:
                command.extend(
                    ["--interest-status", str(arguments["interest_status"])]
                )
            if "related_id" in arguments:
                command.extend(["--related-id", str(arguments["related_id"])])
            if "relation_predicate" in arguments:
                command.extend(
                    [
                        "--relation-predicate",
                        str(arguments["relation_predicate"]),
                    ]
                )
            if "experienced" in arguments:
                command.append(
                    "--experienced"
                    if arguments["experienced"]
                    else "--not-experienced"
                )
            if "min_rating" in arguments:
                command.extend(["--min-rating", str(arguments["min_rating"])])
            if "occurred_after" in arguments:
                command.extend(
                    ["--occurred-after", str(arguments["occurred_after"])]
                )
            if "occurred_before" in arguments:
                command.extend(
                    ["--occurred-before", str(arguments["occurred_before"])]
                )
            if "attribute_filters" in arguments:
                attribute_filters = arguments["attribute_filters"]
                if not isinstance(attribute_filters, dict):
                    raise ToolFailure("attribute_filters must be an object")
                for field, value in sorted(attribute_filters.items()):
                    command.extend(
                        [
                            "--attribute-filter",
                            f"{field}="
                            + json.dumps(
                                value,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ]
                    )
            if "as_of" in arguments:
                command.extend(["--as-of", str(arguments["as_of"])])
            command.extend(["--limit", str(arguments.get("limit", 20))])
            if arguments.get("explain"):
                command.append("--explain")
            command.extend(["--offset", str(arguments.get("offset", 0))])
            command.extend(["--sort", str(arguments.get("sort", "auto"))])
            command.extend(
                ["--rank", str(arguments.get("rank", RANK_HYBRID))]
            )
            return self._run_read(
                argv(
                    "search",
                    options=command,
                    positionals=[str(arguments["query"])],
                )
            )

        if name == "retrieve":
            command = []
            if "has_attachment" in arguments:
                command.append(
                    "--has-attachment" if arguments["has_attachment"]
                    else "--no-has-attachment"
                )
            for variant in arguments.get("query_variants", []):
                # Equals keeps an option-like variant a value, never a CLI flag.
                command.append(f"--query-variant={variant}")
            if "type" in arguments:
                command.extend(["--type", str(arguments["type"])])
            if "limit" in arguments:
                command.extend(["--limit", str(arguments["limit"])])
            if "max_body_chars" in arguments:
                command.extend(
                    ["--max-body-chars", str(arguments["max_body_chars"])]
                )
            if "rank" in arguments:
                command.extend(["--rank", str(arguments["rank"])])
            if "as_of" in arguments:
                command.extend(["--as-of", str(arguments["as_of"])])
            return self._run_read(
                argv(
                    "retrieve",
                    options=command,
                    positionals=[str(arguments["query"])],
                )
            )

        if name == "get_entry":
            command = []
            if "as_of" in arguments:
                command.extend(["--as-of", str(arguments["as_of"])])
            return self._run_read(
                argv(
                    "review",
                    options=command,
                    positionals=[str(arguments["id"])],
                )
            )

        if name == "get_attachment":
            # The entry is read through the same path every other read uses, so
            # what counts as "this entry's attachments" has one definition. That
            # read is also the authorisation: an attachment the entry does not
            # list cannot be addressed, whatever the caller passes.
            entry = self._run_read(
                argv("review", positionals=[str(arguments["id"])])
            )
            attachment = self._entry_attachment(
                entry, str(arguments["attachment_id"])
            )
            data, media_type = read_attachment_blob(
                self.layout.data_root,
                attachment,
                max_bytes=MAX_DELIVERABLE_ATTACHMENT_BYTES,
            )
            summary: dict[str, object] = {
                "id": arguments["id"],
                "attachment_id": arguments["attachment_id"],
                "media_type": media_type,
                "size_bytes": len(data),
                "original_name": attachment.get("original_name"),
                "added_at": attachment.get("added_at"),
            }
            delivery_path = self._place_for_delivery(attachment, data)
            if delivery_path is not None:
                # The host can send the file, so the bytes have no business in
                # the model's request. Returning them anyway put ~177 kB of
                # base64 into a prompt that only needed a path, and the
                # provider rejected the request outright: "Stream must be set
                # to true". The pixels were never for the model to read.
                summary["delivery_path"] = delivery_path
                delivery_marker = self._delivery_marker(delivery_path)
                if delivery_marker is not None:
                    # This is already the host's complete transport token. An
                    # agent can copy it verbatim instead of reconstructing
                    # syntax around a path, which is both faster and less
                    # error-prone across model turns.
                    summary["delivery_marker"] = delivery_marker
                return summary
            return DeliverableImage(
                media_type=media_type,
                data=data,
                summary=summary,
            )

        if name == "review_queue":
            return self._run_read(
                argv(
                    "review",
                    options=["--limit", str(arguments.get("limit", 50))],
                )
            )

        if name == "capture":
            entry_type = str(arguments.get("type", "thought"))
            is_custom_type = entry_type in self.schema_registry.custom_types
            if "text" not in arguments and not is_custom_type:
                raise ToolFailure("capture is missing: text")
            command = [
                "capture",
                "--type",
                entry_type,
                "--sensitivity",
                str(arguments.get("sensitivity", "personal")),
            ]
            if "title" in arguments:
                command.extend(["--title", str(arguments["title"])])
            for alias_value in arguments.get("aliases", []):
                command.extend(["--alias", str(alias_value)])
            for tag_value in arguments.get("tags", []):
                command.extend(["--tag", str(tag_value)])
            for relation_value in arguments.get("relations", []):
                command.extend(
                    ["--relation", self._relation_argument(relation_value)]
                )
            for relation_value in arguments.get("unresolved_relations", []):
                command.extend(
                    [
                        "--unresolved-relation",
                        self._unresolved_argument(relation_value),
                    ]
                )
            if "occurred_at" in arguments:
                command.extend(["--occurred-at", str(arguments["occurred_at"])])
            if "occurred_precision" in arguments:
                command.extend(
                    [
                        "--occurred-precision",
                        str(arguments["occurred_precision"]),
                    ]
                )
            if "place_kind" in arguments:
                command.extend(["--place-kind", str(arguments["place_kind"])])
            if "product_kind" in arguments:
                command.extend(
                    ["--product-kind", str(arguments["product_kind"])]
                )
            if "experience_kind" in arguments:
                command.extend(
                    ["--experience-kind", str(arguments["experience_kind"])]
                )
            if "interest_status" in arguments:
                command.extend(
                    ["--interest-status", str(arguments["interest_status"])]
                )
            if "servings" in arguments:
                command.extend(["--servings", str(arguments["servings"])])
            if "prep_minutes" in arguments:
                command.extend(
                    ["--prep-minutes", str(arguments["prep_minutes"])]
                )
            if "cook_minutes" in arguments:
                command.extend(
                    ["--cook-minutes", str(arguments["cook_minutes"])]
                )
            if "rating" in arguments:
                command.extend(["--rating", str(arguments["rating"])])
            input_text = str(arguments.get("text", ""))
            if "attributes" in arguments:
                attributes = arguments["attributes"]
                if not isinstance(attributes, dict):
                    raise ToolFailure("attributes must be an object")
                return self._run_with_payload(
                    "capture",
                    "--attributes-file",
                    attributes,
                    prefix_arguments=command[1:],
                    input_text=input_text,
                )
            return self._run(command, input_text=input_text)

        if name == "save_recipe":
            command = []
            option_map = {
                "canonical_url": "--canonical-url",
                "title": "--title",
                "text": "--text",
                "site_name": "--site-name",
                "page_description": "--source-description",
                "sensitivity": "--sensitivity",
                "interest_status": "--interest-status",
                "servings": "--servings",
                "prep_minutes": "--prep-minutes",
                "cook_minutes": "--cook-minutes",
            }
            for key, option in option_map.items():
                if key in arguments:
                    command.extend([option, str(arguments[key])])
            for alias_value in arguments.get("aliases", []):
                command.extend(["--alias", str(alias_value)])
            for tag_value in arguments.get("tags", []):
                command.extend(["--tag", str(tag_value)])
            for relation_value in arguments.get("relations", []):
                command.extend(
                    ["--relation", self._relation_argument(relation_value)]
                )
            for relation_value in arguments.get("unresolved_relations", []):
                command.extend(
                    [
                        "--unresolved-relation",
                        self._unresolved_argument(relation_value),
                    ]
                )
            return self._run(
                argv(
                    "recipe",
                    options=command,
                    positionals=[str(arguments["url"])],
                )
            )

        if name == "save_bookmark":
            return self._run_with_payload(
                "bookmark",
                "--metadata-file",
                dict(arguments),
            )

        if name == "refresh_bookmark":
            return self._run_with_payload(
                "refresh-bookmark", "--metadata-file", dict(arguments["envelope"]),
                prefix_arguments=[
                    "--expected-revision", str(arguments["expected_revision"])
                ],
                positionals=[str(arguments["id"])],
            )

        if name == "update":
            payload = {
                key: arguments[key]
                for key in (
                    "title",
                    "aliases",
                    "append",
                    "sensitivity",
                    "bookmark_kind",
                    "record_experience",
                    "experienced_at",
                    "review",
                    "rating",
                    "experience_kind",
                    "occurred_at",
                    "occurred_precision",
                    "source_url",
                    "source_site_name",
                    "source_description",
                    "servings",
                    "prep_minutes",
                    "cook_minutes",
                    "attributes",
                )
                if key in arguments
            }
            if not payload:
                raise ToolFailure(
                    "update requires a supported content or metadata change"
                )
            return self._run_with_payload(
                "update",
                "--patch-file",
                payload,
                prefix_arguments=[
                    "--expected-revision",
                    str(arguments["expected_revision"]),
                ],
                positionals=[str(arguments["id"])],
            )

        if name == "list_pending_attachments":
            return self.list_pending_attachments(
                max_age_seconds=int(
                    arguments.get(
                        "max_age_seconds",
                        DEFAULT_PENDING_ATTACHMENT_AGE_SECONDS,
                    )
                ),
                limit=int(
                    arguments.get(
                        "limit",
                        DEFAULT_PENDING_ATTACHMENT_LIMIT,
                    )
                ),
            )

        if name == "add_attachment":
            has_source_path = "source_path" in arguments
            has_attachment_token = "attachment_token" in arguments
            if has_source_path == has_attachment_token:
                raise ToolFailure(
                    "add_attachment requires exactly one of source_path or "
                    "attachment_token"
                )
            pending: PendingAttachment | None = None
            if has_attachment_token:
                token = str(arguments["attachment_token"])
                pending = self._resolve_pending_attachment(token)
                source = str(pending.path)
            else:
                token = None
                source = str(arguments["source_path"])
            command = [
                "--expected-revision",
                str(arguments["expected_revision"]),
            ]
            if pending is not None:
                command.extend(
                    ["--expected-source-sha256", pending.sha256]
                )
            if "caption" in arguments:
                command.extend(["--caption", str(arguments["caption"])])
            if "experienced_at" in arguments:
                command.extend(
                    ["--experienced-at", str(arguments["experienced_at"])]
                )
            result = self._run(
                argv(
                    "attach",
                    options=command,
                    positionals=[str(arguments["id"]), source],
                )
            )
            if token is not None:
                self._consume_pending_attachment(token)
            return result

        if name == "set_relation":
            command = [
                "--expected-revision",
                str(arguments["expected_revision"]),
            ]
            if arguments["action"] == "remove":
                command.append("--remove")
            if "resolve_reference" in arguments:
                command.extend(
                    ["--resolve-reference", str(arguments["resolve_reference"])]
                )
            # These are option values, and the schema above restricts them to
            # a timestamp shape, so nothing an agent supplies here can be read
            # by argparse as a flag of its own.
            for key, option in (
                ("valid_from", "--valid-from"),
                ("valid_until", "--valid-until"),
                ("supersedes", "--supersedes"),
            ):
                if key in arguments:
                    command.extend([option, str(arguments[key])])
            return self._run(
                argv(
                    "relate",
                    options=command,
                    positionals=[
                        str(arguments["id"]),
                        str(arguments["predicate"]),
                        str(arguments["target_id"]),
                    ],
                )
            )

        if name == "set_unresolved_relation":
            command = [
                "--unresolved",
                "--expected-revision",
                str(arguments["expected_revision"]),
            ]
            if arguments["action"] == "remove":
                command.append("--remove")
            return self._run(
                argv(
                    "relate",
                    options=command,
                    positionals=[
                        str(arguments["id"]),
                        str(arguments["predicate"]),
                        str(arguments["reference"]),
                    ],
                )
            )

        if name == "set_tags":
            command = [
                "--expected-revision",
                str(arguments["expected_revision"]),
            ]
            if arguments["action"] == "remove":
                command.append("--remove")
            return self._run(
                argv(
                    "tag",
                    options=command,
                    positionals=[
                        str(arguments["id"]),
                        *(
                            str(tag_value)
                            for tag_value in arguments["tags"]
                        ),
                    ],
                )
            )

        if name == "set_status":
            command = [
                "--expected-revision",
                str(arguments["expected_revision"]),
            ]
            if "lifecycle" in arguments:
                command.extend(["--lifecycle", str(arguments["lifecycle"])])
            if "reading" in arguments:
                command.extend(["--reading", str(arguments["reading"])])
            if "interest" in arguments:
                command.extend(["--interest", str(arguments["interest"])])
            if not any(
                field in arguments for field in ("lifecycle", "reading", "interest")
            ):
                raise ToolFailure(
                    "set_status requires lifecycle, reading, and/or interest"
                )
            return self._run(
                argv(
                    "status",
                    options=command,
                    positionals=[str(arguments["id"])],
                )
            )

        if name == "complete_review":
            return self._run(
                argv(
                    "review",
                    options=[
                        "--complete",
                        "--expected-revision",
                        str(arguments["expected_revision"]),
                    ],
                    positionals=[str(arguments["id"])],
                )
            )

        if name == "relations":
            command = []
            if "as_of" in arguments:
                command.extend(["--as-of", str(arguments["as_of"])])
            return self._run_read(
                argv(
                    "relations",
                    options=command,
                    positionals=[str(arguments["id"])],
                )
            )

        if name == "list_trash":
            return self._run_read(argv("trash", positionals=["list"]))

        if name == "trash":
            command = [
                "--expected-revision",
                str(arguments["expected_revision"]),
            ]
            if "reason" in arguments:
                command.extend(["--reason", str(arguments["reason"])])
            return self._run(
                argv(
                    "trash",
                    options=command,
                    positionals=[str(arguments["id"])],
                )
            )

        if name == "restore":
            return self._run(
                argv(
                    "restore",
                    options=[
                        "--expected-revision",
                        str(arguments["expected_revision"]),
                    ],
                    positionals=[str(arguments["id"])],
                )
            )

        if name == "validate":
            return self._run(["validate"])

        raise ToolFailure(f"Knowledge tool is not implemented: {name}")


def rpc_response(
    request_id: object,
    *,
    result: object | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def emit(response: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def dispatch(
    server: NoetrailServer, request: object
) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return rpc_response(
            None,
            error={"code": -32600, "message": "Invalid JSON-RPC request"},
        )
    request_id = request.get("id")
    method = request.get("method")
    is_notification = "id" not in request
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        if is_notification:
            return None
        return rpc_response(
            request_id,
            error={"code": -32600, "message": "Invalid JSON-RPC request"},
        )

    instructions = (
        "Use only the exposed structured knowledge operations. "
        "Permanent purge and schema migration are unavailable."
    )
    if method == "initialize":
        return rpc_response(
            request_id,
            result={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "noetrail",
                    "version": SERVER_VERSION,
                },
                "instructions": instructions,
            },
        )
    if method in {
        "notifications/initialized",
        "notifications/cancelled",
    }:
        return None
    try:
        protocol = request_protocol(request)
    except ProtocolRequestError as exc:
        if is_notification:
            return None
        error: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return rpc_response(request_id, error=error)
    if method == "server/discover":
        if protocol != MODERN_PROTOCOL_VERSION:
            return rpc_response(
                request_id,
                error={"code": -32601, "message": f"Method not found: {method}"},
            )
        return rpc_response(
            request_id,
            result=discovery_result(
                name="noetrail",
                version=SERVER_VERSION,
                instructions=instructions,
            ),
        )
    if method == "ping":
        return rpc_response(
            request_id,
            result=result_envelope({}, protocol=protocol),
        )
    if method == "tools/list":
        return rpc_response(
            request_id,
            result=result_envelope(
                {"tools": server.tools},
                protocol=protocol,
                cacheable=True,
            ),
        )
    if method == "tools/call":
        params = request.get("params", {})
        if not isinstance(params, dict):
            return rpc_response(
                request_id,
                error={"code": -32602, "message": "params must be an object"},
            )
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str):
            return rpc_response(
                request_id,
                error={"code": -32602, "message": "Tool name must be a string"},
            )
        failure: ToolFailure | None = None
        try:
            result = server.call_tool(name, arguments)
        except ToolFailure as exc:
            failure = exc
        except NoetrailError as exc:
            # A tool implemented in this process raises the vault's own errors
            # directly, where one delegating to the CLI has them translated in
            # `_run_read`. Untranslated they escaped as an unhandled exception
            # and took the request with them, so the caller saw a transport
            # failure instead of the refusal meant for it.
            failure = ToolFailure(exc.message[:8_000], code=exc.code)
        if failure is not None:
            return rpc_response(
                request_id,
                result=result_envelope(
                    {
                        "content": [{"type": "text", "text": str(failure)}],
                        # Machine-readable counterpart of the text failure.
                        "structuredContent": {
                            "error": {
                                "code": failure.code,
                                "message": str(failure),
                            }
                        },
                        "isError": True,
                    },
                    protocol=protocol,
                ),
            )
        if isinstance(result, DeliverableImage):
            # The one result that is not text. The image block is what a chat
            # channel renders; the summary follows it so a client that cannot
            # render an image still learns what it was handed. The bytes are
            # never written to a log or a path -- they go into this response
            # and nowhere else.
            return rpc_response(
                request_id,
                result=result_envelope(
                    {
                        "content": [
                            {
                                "type": "image",
                                "data": base64.b64encode(result.data).decode(
                                    "ascii"
                                ),
                                "mimeType": result.media_type,
                            },
                            {
                                "type": "text",
                                "text": json.dumps(
                                    result.summary,
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                            },
                        ],
                        "structuredContent": result.summary,
                        "isError": False,
                    },
                    protocol=protocol,
                ),
            )
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return rpc_response(
            request_id,
            result=result_envelope(
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": result,
                    "isError": False,
                },
                protocol=protocol,
            ),
        )
    if is_notification:
        return None
    return rpc_response(
        request_id,
        error={"code": -32601, "message": f"Method not found: {method}"},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve Noetrail's narrow vault tools over MCP stdio"
    )
    parser.add_argument(
        "--root",
        help="legacy combined root containing .knowledge, vault, and trash",
    )
    parser.add_argument(
        "--data-root",
        help="private data root containing vault, trash, and imports",
    )
    parser.add_argument(
        "--config-root",
        help="instance configuration root for local packs and overrides",
    )
    parser.add_argument(
        "--builtins-root",
        help="read-only built-in specifications, schemas, and templates",
    )
    parser.add_argument(
        "--know-script",
        help="path to the Noetrail CLI module; defaults to the sibling cli.py",
    )
    parser.add_argument(
        "--attachment-inbox",
        help="fixed directory containing inbound channel attachments",
    )
    parser.add_argument(
        "--attachment-outbox",
        help=(
            "fixed directory a stored image is copied into for the host to "
            "deliver; point it inside the workspace the channel sends from"
        ),
    )
    parser.add_argument(
        "--attachment-delivery-marker-template",
        help=(
            "optional one-line host delivery syntax containing {path} exactly "
            "once; requires --attachment-outbox and is returned verbatim as "
            "delivery_marker"
        ),
    )
    parser.add_argument(
        "--attachment-delivery-marker-root",
        help=(
            "optional host workspace root; when set, {path} in the delivery "
            "marker is relative to this root and the attachment outbox must "
            "be inside it"
        ),
    )
    parser.add_argument(
        "--subprocess-reads",
        action="store_true",
        help=(
            "answer read operations in a separate CLI process instead of this "
            "one; slower, and kept as a fallback"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    know_script = (
        Path(args.know_script).expanduser() if args.know_script else None
    )
    attachment_inbox = (
        Path(args.attachment_inbox).expanduser()
        if args.attachment_inbox
        else None
    )
    attachment_outbox = (
        Path(args.attachment_outbox).expanduser()
        if args.attachment_outbox
        else None
    )
    attachment_delivery_marker_template = args.attachment_delivery_marker_template
    attachment_delivery_marker_root = (
        Path(args.attachment_delivery_marker_root).expanduser()
        if args.attachment_delivery_marker_root
        else None
    )
    try:
        layout = resolve_layout(
            root=args.root,
            data_root=args.data_root,
            config_root=args.config_root,
            builtins_root=args.builtins_root,
        )
    except LayoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        server = NoetrailServer(
            layout,
            know_script,
            attachment_inbox,
            attachment_outbox,
            in_process_reads=not args.subprocess_reads,
            attachment_delivery_marker_template=(
                attachment_delivery_marker_template
            ),
            attachment_delivery_marker_root=attachment_delivery_marker_root,
        )
    except NoetrailError as exc:
        # Same shape as the CLI: the sentence on stderr, a non-zero status.
        print(exc.message, file=sys.stderr)
        return exc.exit_code

    for line in sys.stdin:
        if not line.strip():
            continue
        if len(line) > MAX_REQUEST_CHARS:
            emit(
                rpc_response(
                    None,
                    error={"code": -32700, "message": "Request is too large"},
                )
            )
            continue
        try:
            request = loads_bounded(line)
        except JsonRequestError:
            emit(
                rpc_response(
                    None,
                    error={"code": -32700, "message": "Parse error"},
                )
            )
            continue
        response = dispatch(server, request)
        if response is not None:
            emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""The entry-creating commands: capture, recipe, bookmark."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import uuid

from noetrail.body import fence_web_content, heading_for
from noetrail.constants import (
    BOOKMARK_KINDS,
    CAPTURE_TYPES,
    FETCH_STATUSES,
    INTEREST_TYPES,
    MAX_RECIPE_MINUTES,
    READING_STATUSES,
    SENSITIVITIES,
)
from noetrail.entries import duplicate_bookmarks, duplicate_recipes, duplicate_titles
from noetrail.errors import (
    Conflict,
    InvalidRequest,
)
from noetrail.frontmatter import pack_builtin_metadata
from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
)
from noetrail.normalize import (
    canonicalize_url,
    infer_title,
    normalize_alias_list,
    normalize_tag_list,
    now_iso,
    slugify,
)
from noetrail.payloads import load_attributes_payload, load_bookmark_payload
from noetrail.provenance import bookmark_provenance, with_provenance
from noetrail.relations import (
    parse_relations,
    parse_unresolved_relations,
    validate_relation_objects,
    validate_unresolved_relation_objects,
)
from noetrail.schema import SchemaPackError
from noetrail.store import (
    atomic_write_entry,
    entry_directory,
    parse_timestamp,
    prepare_custom_entry_directory,
    revision_for,
)


def command_capture(args: argparse.Namespace, root: Path) -> int:
    custom_type = None
    if args.type not in CAPTURE_TYPES:
        if args.type == "bookmark":
            raise InvalidRequest("Use the bookmark command to capture bookmarks")
        try:
            custom_type = args.registry.require_type(args.type)
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc

    if custom_type is None:
        if args.attributes_file is not None:
            raise InvalidRequest(
                "--attributes-file is only valid for pack-defined types"
            )
        attributes = None
    else:
        try:
            attributes = custom_type.validate_attributes(
                load_attributes_payload(args.attributes_file),
                apply_defaults=True,
            )
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc

    supplied_text = args.text if args.text is not None else sys.stdin.read()
    text = supplied_text
    if custom_type is not None and not text.strip():
        text = custom_type.initial_body()
    if custom_type is None and not text.strip():
        raise InvalidRequest("Capture text is empty")
    title = (args.title or infer_title(text)).strip()
    if not title:
        raise InvalidRequest("Title is empty")
    try:
        aliases = normalize_alias_list(args.alias, title)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc

    duplicates = (
        []
        if args.type == "experience"
        else duplicate_titles(args.index, args.type, title)
    )
    if duplicates and not args.allow_duplicate:
        listed = ", ".join(str(path.relative_to(root)) for path in duplicates)
        raise Conflict(
            f"An entry with this type and title already exists: {listed}. "
            "Update it or pass --allow-duplicate after review."
        )

    if args.type not in {"memory", "experience"} and (
        args.occurred_at or args.occurred_precision
    ):
        raise InvalidRequest(
            "occurred fields are only valid for memories and experiences"
        )
    if (
        args.type != "experience"
        and args.occurred_precision
        and not args.occurred_at
    ):
        raise InvalidRequest("--occurred-precision requires --occurred-at")
    if args.type == "place":
        if args.place_kind is None:
            raise InvalidRequest("--place-kind is required for places")
        if args.product_kind is not None:
            raise InvalidRequest("--product-kind is only valid for products")
    elif args.place_kind is not None:
        raise InvalidRequest("--place-kind is only valid for places")
    if args.type == "product":
        if args.product_kind is None:
            raise InvalidRequest("--product-kind is required for products")
    elif args.product_kind is not None:
        raise InvalidRequest("--product-kind is only valid for products")
    recipe_fields_supplied = any(
        value is not None
        for value in (args.servings, args.prep_minutes, args.cook_minutes)
    )
    if args.type != "recipe" and recipe_fields_supplied:
        raise InvalidRequest(
            "servings and recipe time fields are only valid for recipes"
        )
    if args.type == "recipe" and args.servings is not None:
        if not args.servings.strip() or len(args.servings.strip()) > 200:
            raise InvalidRequest("servings must be 1 to 200 characters")
    for field, value in (
        ("prep_minutes", args.prep_minutes),
        ("cook_minutes", args.cook_minutes),
    ):
        if value is not None and (value < 0 or value > MAX_RECIPE_MINUTES):
            raise InvalidRequest(
                f"{field} must be from 0 to {MAX_RECIPE_MINUTES}"
            )
    if args.type not in INTEREST_TYPES and args.interest_status is not None:
        raise InvalidRequest(
            "--interest-status is only valid for places, products, and recipes"
        )
    if args.type == "experience":
        if args.experience_kind is None:
            raise InvalidRequest("--experience-kind is required for experiences")
        if args.occurred_at is not None:
            parse_timestamp(args.occurred_at, "occurred_at")
    elif args.experience_kind is not None:
        raise InvalidRequest("--experience-kind is only valid for experiences")
    if args.rating is not None:
        if args.type != "experience":
            raise InvalidRequest("--rating is only valid for experiences")
        if args.rating < 1 or args.rating > 5:
            raise InvalidRequest("--rating must be an integer from 1 to 5")

    entry_id = f"kn_{uuid.uuid4().hex}"
    timestamp = now_iso()
    metadata: dict[str, object] = {
        "id": entry_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": args.type,
        "title": title,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "unreviewed" if args.type == "thought" else "active",
        "sensitivity": args.sensitivity,
        "tags": normalize_tag_list(args.tag),
        "relations": parse_relations(args.relation, args.index, args.layout),
        "origin": "conversation",
    }
    if aliases:
        metadata["aliases"] = aliases
    if custom_type is not None:
        metadata["type_version"] = custom_type.version
        metadata["attributes"] = attributes
    unresolved_relations = parse_unresolved_relations(
        args.unresolved_relation, args.layout
    )
    if unresolved_relations:
        metadata["unresolved_relations"] = unresolved_relations
    if args.type == "memory" and args.occurred_at:
        metadata["occurred_at"] = args.occurred_at
        metadata["occurred_precision"] = args.occurred_precision or "unknown"
    if args.type == "experience":
        metadata["experience_kind"] = args.experience_kind
        metadata["occurred_at"] = args.occurred_at or timestamp
        metadata["occurred_precision"] = args.occurred_precision or "datetime"
        if args.rating is not None:
            metadata["rating"] = args.rating
    if args.type == "place":
        metadata["place_kind"] = args.place_kind
        metadata["interest_status"] = args.interest_status or "none"
    if args.type == "product":
        metadata["product_kind"] = args.product_kind
        metadata["interest_status"] = args.interest_status or "none"
    if args.type == "recipe":
        metadata["interest_status"] = args.interest_status or "none"
        if args.servings is not None:
            metadata["servings"] = args.servings.strip()
        if args.prep_minutes is not None:
            metadata["prep_minutes"] = args.prep_minutes
        if args.cook_minutes is not None:
            metadata["cook_minutes"] = args.cook_minutes

    if custom_type is not None:
        destination = prepare_custom_entry_directory(
            root,
            args.type,
            args.registry,
        )
    else:
        metadata = pack_builtin_metadata(metadata, args.registry)
        destination = entry_directory(root, args.type)
        destination.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{datetime.now().astimezone().date()}"
        f"--{slugify(title)}--{entry_id[3:11]}.md"
    )
    path = destination / filename
    atomic_write_entry(path, metadata, text, must_not_exist=True)

    print(
        json.dumps(
            {
                "created": str(path.relative_to(root)),
                "id": entry_id,
                "title": title,
                "type": args.type,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_recipe(args: argparse.Namespace, root: Path) -> int:
    try:
        canonicalize_url(args.url)
        source_url = canonicalize_url(args.canonical_url or args.url)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc

    duplicates = duplicate_recipes(args.index, source_url)
    if duplicates:
        listed = ", ".join(str(path.relative_to(root)) for path in duplicates)
        raise Conflict(f"Recipe source URL already exists: {listed}")

    text = args.text or ""
    if args.title:
        title = args.title.strip()
    elif text.strip():
        title = infer_title(text).strip()
    else:
        title = (urlsplit(source_url).hostname or "Recipe").strip()
    if not title:
        raise InvalidRequest("Recipe title is empty")
    try:
        aliases = normalize_alias_list(args.alias, title)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if args.servings is not None:
        servings = args.servings.strip()
        if not servings or len(servings) > 200:
            raise InvalidRequest("servings must be 1 to 200 characters")
    else:
        servings = None
    for field, value in (
        ("prep_minutes", args.prep_minutes),
        ("cook_minutes", args.cook_minutes),
    ):
        if value is not None and (value < 0 or value > MAX_RECIPE_MINUTES):
            raise InvalidRequest(
                f"{field} must be from 0 to {MAX_RECIPE_MINUTES}"
            )

    entry_id = f"kn_{uuid.uuid4().hex}"
    timestamp = now_iso()
    source_domain = urlsplit(source_url).hostname
    metadata: dict[str, object] = {
        "id": entry_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": "recipe",
        "title": title,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "active",
        "sensitivity": args.sensitivity,
        "tags": normalize_tag_list(args.tag),
        "relations": parse_relations(args.relation, args.index, args.layout),
        "origin": "conversation",
        "interest_status": args.interest_status,
        "source_url": source_url,
        "source_domain": source_domain,
    }
    if aliases:
        metadata["aliases"] = aliases
    unresolved_relations = parse_unresolved_relations(
        args.unresolved_relation, args.layout
    )
    if unresolved_relations:
        metadata["unresolved_relations"] = unresolved_relations
    if args.site_name:
        site_name = args.site_name.strip()
        if not site_name:
            raise InvalidRequest("site_name must be a non-empty string")
        metadata["source_site_name"] = site_name
    if args.source_description:
        source_description = args.source_description.strip()
        if not source_description:
            raise InvalidRequest("source_description must be a non-empty string")
        metadata["source_description"] = source_description
    if servings is not None:
        metadata["servings"] = servings
    if args.prep_minutes is not None:
        metadata["prep_minutes"] = args.prep_minutes
    if args.cook_minutes is not None:
        metadata["cook_minutes"] = args.cook_minutes

    metadata = pack_builtin_metadata(metadata, args.registry)
    destination = entry_directory(root, "recipe")
    destination.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{datetime.now().astimezone().date()}--{slugify(title)}--"
        f"{entry_id[3:11]}.md"
    )
    path = destination / filename
    atomic_write_entry(path, metadata, text, must_not_exist=True)
    print(
        json.dumps(
            {
                "created": str(path.relative_to(root)),
                "id": entry_id,
                "title": title,
                "type": "recipe",
                "source_url": source_url,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_bookmark(args: argparse.Namespace, root: Path) -> int:
    payload = load_bookmark_payload(args.metadata_file)
    url_value = args.url or payload.get("url")
    if not isinstance(url_value, str):
        raise InvalidRequest("Bookmark URL is required")
    canonical_value = payload.get("canonical_url", url_value)
    if not isinstance(canonical_value, str):
        raise InvalidRequest("canonical_url must be a string")
    try:
        url = canonicalize_url(url_value)
        canonical_url = canonicalize_url(canonical_value)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc

    duplicates = duplicate_bookmarks(args.index, canonical_url)
    if duplicates and not args.allow_duplicate:
        listed = ", ".join(str(path.relative_to(root)) for path in duplicates)
        raise Conflict(f"Bookmark already exists: {listed}")

    domain = urlsplit(canonical_url).hostname
    title_value = payload.get("title") or domain
    if not isinstance(title_value, str) or not title_value.strip():
        raise InvalidRequest("Bookmark title must be a non-empty string")
    title = title_value.strip()

    tags = payload.get("tags", [])
    aliases_value = payload.get("aliases", [])
    authors = payload.get("authors", [])
    relations = payload.get("relations", [])
    unresolved_relations = payload.get("unresolved_relations", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise InvalidRequest("Bookmark tags must be an array of strings")
    if not isinstance(aliases_value, list) or not all(
        isinstance(alias, str) for alias in aliases_value
    ):
        raise InvalidRequest("Bookmark aliases must be an array of strings")
    try:
        aliases = normalize_alias_list(aliases_value, title)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if not isinstance(authors, list) or not all(
        isinstance(author, str) for author in authors
    ):
        raise InvalidRequest("Bookmark authors must be an array of strings")
    if not isinstance(relations, list):
        raise InvalidRequest("Bookmark relations must be an array")
    if not isinstance(unresolved_relations, list):
        raise InvalidRequest("Bookmark unresolved_relations must be an array")

    reading_status = payload.get("reading_status", "unread")
    bookmark_kind = payload.get("bookmark_kind", "unknown")
    fetch_status = payload.get("fetch_status", "not_attempted")
    sensitivity = payload.get("sensitivity", "personal")
    if reading_status not in READING_STATUSES:
        raise InvalidRequest(f"Invalid reading_status: {reading_status}")
    if bookmark_kind not in BOOKMARK_KINDS:
        raise InvalidRequest(f"Invalid bookmark_kind: {bookmark_kind}")
    if fetch_status not in FETCH_STATUSES:
        raise InvalidRequest(f"Invalid fetch_status: {fetch_status}")
    if sensitivity not in SENSITIVITIES:
        raise InvalidRequest(f"Invalid sensitivity: {sensitivity}")

    timestamp = now_iso()
    entry_id = f"kn_{uuid.uuid4().hex}"
    metadata: dict[str, object] = {
        "id": entry_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": "bookmark",
        "title": title,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "active",
        "sensitivity": sensitivity,
        "tags": sorted(set(tags)),
        "relations": validate_relation_objects(relations, args.index, args.layout),
        "origin": "conversation",
        "url": url,
        "canonical_url": canonical_url,
        "domain": domain,
        "retrieved_at": timestamp,
        "reading_status": reading_status,
        "bookmark_kind": bookmark_kind,
        "fetch_status": fetch_status,
    }
    if aliases:
        metadata["aliases"] = aliases
    if reading_status == "read":
        metadata["read_at"] = timestamp
    validated_unresolved = validate_unresolved_relation_objects(
        unresolved_relations, args.layout
    )
    if validated_unresolved:
        metadata["unresolved_relations"] = validated_unresolved
    for field in (
        "site_name",
        "published_at",
        "language",
        "page_description",
    ):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise InvalidRequest(f"{field} must be a string")
            metadata[field] = value
    if authors:
        metadata["authors"] = authors

    # Per field, not per entry: `title` can come off the page while `note` and
    # `tags` come from the user, and an entry-level flag cannot say that.
    provenance = {
        name: origin
        for name, origin in bookmark_provenance(payload).items()
        if name in metadata
    }
    metadata = pack_builtin_metadata(metadata, args.registry)
    metadata = with_provenance(metadata, provenance)

    headings = args.body_headings
    body_parts = [
        f"## {heading_for(headings, 'link')}",
        "",
        f"[Open the original page]({canonical_url})",
    ]
    note = payload.get("note")
    summary = payload.get("summary")
    if note is not None:
        if not isinstance(note, str):
            raise InvalidRequest("note must be a string")
        if note.strip():
            body_parts.extend(
                ["", f"## {heading_for(headings, 'personal_note')}", "", note.strip()]
            )
    if summary is not None:
        if not isinstance(summary, str):
            raise InvalidRequest("summary must be a string")
        if summary.strip():
            # The heading alone never made this boundary machine-readable: it
            # is configurable prose, it was German until 0.10.0a1, and a
            # personal note may legitimately contain the same words. The fence
            # is what a reader can rely on.
            body_parts.append("")
            body_parts.extend(
                fence_web_content(
                    "\n".join(
                        [
                            f"## {heading_for(headings, 'generated_summary')}",
                            "",
                            summary.strip(),
                        ]
                    )
                )
            )

    destination = entry_directory(root, "bookmark")
    destination.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{datetime.now().astimezone().date()}"
        f"--{slugify(title)}--{entry_id[3:11]}.md"
    )
    path = destination / filename
    atomic_write_entry(
        path,
        metadata,
        "\n".join(body_parts),
        must_not_exist=True,
    )

    print(
        json.dumps(
            {
                "created": str(path.relative_to(root)),
                "id": entry_id,
                "title": title,
                "type": "bookmark",
                "canonical_url": canonical_url,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0

#!/usr/bin/env python3
"""The entry-editing commands: update, attach, relate, tag, status."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from noetrail.attachments import (
    attachment_inbox_path,
    read_attachment_source,
    write_attachment_blob,
)
from noetrail.body import heading_for
from noetrail.constants import (
    ATTACHMENT_SHA256_PATTERN,
    BOOKMARK_KINDS,
    ENTRY_TYPES,
    EXPERIENCE_KINDS,
    INTEREST_TYPES,
    LEGACY_EXPERIENCE_TYPES,
    MAX_ATTACHMENTS_PER_ENTRY,
    MAX_RECIPE_MINUTES,
    PRECISIONS,
    SENSITIVITIES,
)
from noetrail.entries import duplicate_recipes, duplicate_titles, find_entry
from noetrail.errors import (
    Conflict,
    InvalidRequest,
    NoChange,
    NotFound,
    ValidationError,
)
from noetrail.frontmatter import (
    entry_type_name,
    metadata_list,
    require_current_schema,
    require_supported_entry_type,
)
from noetrail.normalize import (
    canonicalize_url,
    normalize_alias_list,
    now_iso,
    slugify,
    tag_key,
)
from noetrail.payloads import load_update_payload
from noetrail.provenance import drop_provenance
from noetrail.relations import (
    load_relation_definitions,
    validate_relation_objects,
    validate_unresolved_relation_objects,
)
from noetrail.schema import SchemaPackError
from noetrail.store import (
    atomic_write_entry,
    entry_directory,
    parse_timestamp,
    prepare_custom_entry_directory,
    relocate_entry,
    require_expected_revision,
    revision_for,
)
from noetrail.temporal import (
    apply_relation_validity,
    check_validity_arguments,
    find_edge,
)


def command_update(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_expected_revision(path, args.expected_revision)
    payload = load_update_payload(args.patch_file)
    changed: list[str] = []
    entry_type = entry_type_name(metadata)
    custom_type = (
        args.registry.custom_types.get(entry_type)
        if entry_type is not None
        else None
    )
    if entry_type not in ENTRY_TYPES and custom_type is None:
        raise ValidationError(
            f"Entry uses an unavailable schema type: {metadata.get('type')}"
        )

    current_attributes: dict[str, object] | None = None
    if custom_type is not None:
        try:
            current_attributes = custom_type.validate_attributes(
                metadata.get("attributes"),
                apply_defaults=False,
            )
        except SchemaPackError as exc:
            raise ValidationError(
                f"Entry attributes are invalid; run validate: {exc}"
            ) from exc

    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or not title.strip():
            raise InvalidRequest("title must be a non-empty string")
        title = title.strip()
        duplicates = (
            []
            if metadata.get("type") == "experience"
            else [
                duplicate
                for duplicate in duplicate_titles(
                    args.index, str(metadata.get("type")), title
                )
                if duplicate != path
            ]
        )
        if duplicates:
            listed = ", ".join(str(item.relative_to(root)) for item in duplicates)
            raise Conflict(
                f"Another entry already uses this type and title: {listed}"
            )
        if title != metadata.get("title"):
            metadata["title"] = title
            changed.append("title")

    aliases_value = payload.get("aliases", metadata.get("aliases", []))
    if not isinstance(aliases_value, list) or not all(
        isinstance(alias, str) for alias in aliases_value
    ):
        raise InvalidRequest("aliases must be an array of strings")
    try:
        aliases = normalize_alias_list(
            aliases_value,
            str(metadata.get("title", "")),
        )
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if "aliases" in payload and aliases != metadata.get("aliases", []):
        if aliases:
            metadata["aliases"] = aliases
        else:
            metadata.pop("aliases", None)
        changed.append("aliases")

    if "sensitivity" in payload:
        sensitivity = payload["sensitivity"]
        if sensitivity not in SENSITIVITIES:
            raise InvalidRequest(f"Invalid sensitivity: {sensitivity}")
        if sensitivity != metadata.get("sensitivity"):
            metadata["sensitivity"] = sensitivity
            changed.append("sensitivity")

    if "attributes" in payload:
        if custom_type is None or current_attributes is None:
            raise InvalidRequest(
                "attributes are only valid for pack-defined entry types"
            )
        attribute_patch = payload["attributes"]
        if not isinstance(attribute_patch, dict):
            raise InvalidRequest("attributes update must be an object")
        unknown = sorted(set(attribute_patch) - set(custom_type.fields))
        if unknown:
            raise InvalidRequest(
                f"attributes for {custom_type.qualified_id} contain unknown "
                f"fields: {', '.join(str(name) for name in unknown)}"
            )
        merged_attributes = dict(current_attributes)
        for name, value in attribute_patch.items():
            if value is None:
                merged_attributes.pop(name, None)
            else:
                merged_attributes[name] = value
        try:
            merged_attributes = custom_type.validate_attributes(
                merged_attributes,
                apply_defaults=False,
            )
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc
        if merged_attributes != current_attributes:
            metadata["attributes"] = merged_attributes
            changed.append("attributes")
            drop_provenance(metadata, *attribute_patch)

    if "bookmark_kind" in payload:
        if metadata.get("type") != "bookmark":
            raise InvalidRequest("bookmark_kind is only valid for bookmarks")
        bookmark_kind = payload["bookmark_kind"]
        if bookmark_kind not in BOOKMARK_KINDS:
            raise InvalidRequest(f"Invalid bookmark_kind: {bookmark_kind}")
        if bookmark_kind != metadata.get("bookmark_kind"):
            metadata["bookmark_kind"] = bookmark_kind
            changed.append("bookmark_kind")

    recipe_fields = {
        "source_url",
        "source_site_name",
        "source_description",
        "servings",
        "prep_minutes",
        "cook_minutes",
    }
    supplied_recipe_fields = recipe_fields & payload.keys()
    if supplied_recipe_fields and metadata.get("type") != "recipe":
        raise InvalidRequest("recipe metadata fields are only valid for recipes")
    if "source_url" in payload:
        source_url_value = payload["source_url"]
        if not isinstance(source_url_value, str):
            raise InvalidRequest("source_url must be a string")
        try:
            source_url = canonicalize_url(source_url_value)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        duplicates = [
            duplicate
            for duplicate in duplicate_recipes(args.index, source_url)
            if duplicate != path
        ]
        if duplicates:
            listed = ", ".join(str(item.relative_to(root)) for item in duplicates)
            raise Conflict(f"Recipe source URL already exists: {listed}")
        if source_url != metadata.get("source_url"):
            metadata["source_url"] = source_url
            metadata["source_domain"] = urlsplit(source_url).hostname
            changed.extend(["source_url", "source_domain"])
    for field in ("source_site_name", "source_description", "servings"):
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequest(f"{field} must be a non-empty string")
        value = value.strip()
        maximum = 200 if field == "servings" else 10_000
        if len(value) > maximum:
            raise InvalidRequest(f"{field} is longer than {maximum} characters")
        if value != metadata.get(field):
            metadata[field] = value
            changed.append(field)
    for field in ("prep_minutes", "cook_minutes"):
        if field not in payload:
            continue
        value = payload[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_RECIPE_MINUTES
        ):
            raise InvalidRequest(
                f"{field} must be an integer from 0 to {MAX_RECIPE_MINUTES}"
            )
        if value != metadata.get(field):
            metadata[field] = value
            changed.append(field)

    is_trackable_entry = entry_type in LEGACY_EXPERIENCE_TYPES
    is_structured_experience = entry_type == "experience"
    record_experience = payload.get("record_experience", False)
    if not isinstance(record_experience, bool):
        raise InvalidRequest("record_experience must be a boolean")
    if "record_experience" in payload and not record_experience:
        raise InvalidRequest("record_experience must be true when supplied")
    if any(field in payload for field in ("review", "experienced_at")):
        if not record_experience:
            raise InvalidRequest(
                "review and experienced_at require record_experience true"
            )
    if any(
        field in payload
        for field in ("record_experience", "review", "experienced_at")
    ) and not is_trackable_entry:
        raise InvalidRequest(
            "record_experience, review, and experienced_at are only valid "
            "for legacy place/product experience updates"
        )

    structured_fields = {"experience_kind", "occurred_at", "occurred_precision"}
    supplied_structured_fields = structured_fields & payload.keys()
    if supplied_structured_fields and not is_structured_experience:
        raise InvalidRequest(
            "experience_kind and occurred fields are only valid for experiences"
        )
    if "experience_kind" in payload:
        experience_kind = payload["experience_kind"]
        if experience_kind not in EXPERIENCE_KINDS:
            raise InvalidRequest(f"Invalid experience_kind: {experience_kind}")
        if experience_kind != metadata.get("experience_kind"):
            metadata["experience_kind"] = experience_kind
            changed.append("experience_kind")
    if "occurred_at" in payload:
        occurred_at = payload["occurred_at"]
        parse_timestamp(occurred_at, "occurred_at")
        if occurred_at != metadata.get("occurred_at"):
            metadata["occurred_at"] = occurred_at
            changed.append("occurred_at")
    if "occurred_precision" in payload:
        occurred_precision = payload["occurred_precision"]
        if occurred_precision not in PRECISIONS:
            raise InvalidRequest(f"Invalid occurred_precision: {occurred_precision}")
        if occurred_precision != metadata.get("occurred_precision"):
            metadata["occurred_precision"] = occurred_precision
            changed.append("occurred_precision")

    if "rating" in payload:
        if not (is_trackable_entry or is_structured_experience):
            raise InvalidRequest(
                "rating is only valid for places, products, and experiences"
            )
        rating = payload["rating"]
        if isinstance(rating, bool) or not isinstance(rating, int):
            raise InvalidRequest("rating must be an integer from 1 to 5")
        if rating < 1 or rating > 5:
            raise InvalidRequest("rating must be an integer from 1 to 5")
        if (
            is_trackable_entry
            and not record_experience
            and "last_experienced_at" not in metadata
        ):
            raise InvalidRequest("rating requires a recorded experience")
        if rating != metadata.get("rating"):
            metadata["rating"] = rating
            changed.append("rating")

    if record_experience:
        experienced_at_value = payload.get("experienced_at")
        if experienced_at_value is None:
            experienced_at = now_iso()
            experienced_timestamp = datetime.fromisoformat(experienced_at)
        else:
            experienced_timestamp = parse_timestamp(
                experienced_at_value, "experienced_at"
            )
            assert isinstance(experienced_at_value, str)
            experienced_at = experienced_at_value
        metadata["last_experienced_at"] = experienced_at
        changed.append("last_experienced_at")
        if metadata.get("interest_status") != "none":
            metadata["interest_status"] = "none"
            changed.append("interest_status")
        review = payload.get("review")
        if review is not None:
            if not isinstance(review, str) or not review.strip():
                raise InvalidRequest("review must be a non-empty string")
            section = (
                f"## {heading_for(args.body_headings, 'personal_rating')} — "
                f"{experienced_timestamp.date().isoformat()}\n\n{review.strip()}"
            )
            body = f"{body.rstrip()}\n\n{section}".strip()
            changed.append("review")

    has_append = "append" in payload
    has_replacement = "replace_body" in payload
    if has_append and has_replacement:
        raise InvalidRequest("append and replace_body cannot be used together")
    if record_experience and has_replacement:
        raise InvalidRequest(
            "record_experience and replace_body cannot be used together"
        )
    if has_append:
        addition = payload["append"]
        if not isinstance(addition, str) or not addition.strip():
            raise InvalidRequest("append must be a non-empty string")
        body = f"{body.rstrip()}\n\n{addition.strip()}".strip()
        changed.append("body")
    if has_replacement:
        replacement = payload["replace_body"]
        if not isinstance(replacement, str):
            raise InvalidRequest("replace_body must be a string")
        if not args.allow_replace_body:
            raise InvalidRequest(
                "Replacing personal content requires --allow-replace-body "
                "after explicit user approval"
            )
        if replacement != body:
            body = replacement
            changed.append("body")

    if not changed:
        raise NoChange("Update would not change the entry")
    # The user supplied every value in `changed`, so an origin recorded for it
    # at capture time no longer describes what is stored.
    drop_provenance(metadata, *changed)
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
        )
    )
    return 0


def command_attach(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)

    attachments = metadata.get("attachments", [])
    if not isinstance(attachments, list):
        raise ValidationError("Entry attachments are invalid; run validate")
    if len(attachments) >= MAX_ATTACHMENTS_PER_ENTRY:
        raise InvalidRequest(
            f"Entry already has the maximum of {MAX_ATTACHMENTS_PER_ENTRY} attachments"
        )

    caption: str | None = None
    if args.caption is not None:
        caption = args.caption.strip()
        if not caption:
            raise InvalidRequest("Attachment caption must be non-empty when supplied")
        if len(caption) > 2_000:
            raise InvalidRequest("Attachment caption is longer than 2000 characters")

    experienced_at: str | None = None
    if args.experienced_at is not None:
        if metadata.get("type") not in LEGACY_EXPERIENCE_TYPES:
            raise InvalidRequest(
                "Attachment experienced_at is only valid for places and products"
            )
        if "last_experienced_at" not in metadata:
            raise InvalidRequest(
                "Attachment experienced_at requires a recorded experience"
            )
        parse_timestamp(args.experienced_at, "experienced_at")
        experienced_at = args.experienced_at

    inbox = attachment_inbox_path(args.attachment_inbox)
    data, original_name, media_type, extension = read_attachment_source(
        args.source,
        inbox,
    )
    digest = hashlib.sha256(data).hexdigest()
    if args.expected_source_sha256 is not None:
        if not ATTACHMENT_SHA256_PATTERN.fullmatch(
            args.expected_source_sha256
        ):
            raise InvalidRequest("Expected attachment source SHA-256 is invalid")
        if digest != args.expected_source_sha256:
            raise Conflict(
                "Attachment source changed after it was listed; list pending "
                "attachments again"
            )
    for existing_attachment in attachments:
        if (
            isinstance(existing_attachment, dict)
            and existing_attachment.get("sha256") == digest
        ):
            raise Conflict("This image is already attached to the entry")

    destination = write_attachment_blob(root, data, digest, extension)
    relative_destination = destination.relative_to(root).as_posix()
    timestamp = now_iso()
    attachment: dict[str, object] = {
        "id": f"ka_{uuid.uuid4().hex}",
        "path": relative_destination,
        "media_type": media_type,
        "sha256": digest,
        "size_bytes": len(data),
        "original_name": original_name,
        "added_at": timestamp,
        "origin": "attachment_inbox",
    }
    if caption is not None:
        attachment["caption"] = caption
    if experienced_at is not None:
        attachment["experienced_at"] = experienced_at

    metadata["attachments"] = [*attachments, attachment]
    metadata["updated_at"] = timestamp
    atomic_write_entry(path, metadata, body)
    print(
        json.dumps(
            {
                "updated": str(path.relative_to(root)),
                "id": args.id,
                "changed": ["attachments"],
                "attachment": attachment,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_relate(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)
    relations = metadata.get("relations")
    if not isinstance(relations, list):
        raise ValidationError("Entry relations are invalid; run validate")

    unresolved_values = metadata.get("unresolved_relations", [])
    if not isinstance(unresolved_values, list):
        raise ValidationError("Entry unresolved_relations are invalid; run validate")
    unresolved = validate_unresolved_relation_objects(
        unresolved_values, args.layout
    )
    check_validity_arguments(
        valid_from=args.valid_from,
        valid_until=args.valid_until,
        supersedes=args.supersedes,
        adding=not (args.remove or args.unresolved),
    )

    if args.unresolved:
        if args.resolve_reference:
            raise InvalidRequest(
                "--unresolved and --resolve-reference cannot be used together"
            )
        pending = validate_unresolved_relation_objects(
            [{"predicate": args.predicate, "reference": args.target}],
            args.layout,
        )[0]
        if args.remove:
            if pending not in unresolved:
                raise NotFound("Unresolved relation does not exist")
            unresolved = [item for item in unresolved if item != pending]
            action = "unresolved_removed"
        else:
            if pending in unresolved:
                raise Conflict("Unresolved relation already exists")
            unresolved.append(pending)
            action = "unresolved_added"
        if unresolved:
            metadata["unresolved_relations"] = unresolved
        else:
            metadata.pop("unresolved_relations", None)
        metadata["updated_at"] = now_iso()
        atomic_write_entry(path, metadata, body)
        print(
            json.dumps(
                {
                    "updated": str(path.relative_to(root)),
                    "id": args.id,
                    "unresolved_relation": pending,
                    "action": action,
                    "revision": revision_for(path),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.remove and args.resolve_reference:
        raise InvalidRequest("--resolve-reference is only valid when adding a relation")
    if args.id == args.target:
        raise InvalidRequest("Self-relations are not supported")
    relation: dict[str, object] = {
        "predicate": args.predicate,
        "target": args.target,
    }
    unlinked: list[object] = []

    if args.remove:
        # Identity is `(predicate, target)`, not the whole object: a relation
        # that carries an interval is still the same assertion, and comparing
        # dictionaries would stop finding it the moment it gained one.
        existing = find_edge(relations, args.predicate, args.target)
        if existing is None:
            raise NotFound("Relation does not exist")
        remaining = [item for item in relations if item is not existing]
        for item in remaining:
            if (
                isinstance(item, dict)
                and item.get("predicate") == args.predicate
                and item.get("superseded_by") == args.target
            ):
                # The predecessor's `valid_until` stays -- the statement did
                # stop being asserted then. Only the pointer at a replacement
                # that no longer exists is dropped, because a dangling one
                # would make the entry unvalidatable through a supported edit.
                item.pop("superseded_by", None)
                unlinked.append(item.get("target"))
        metadata["relations"] = remaining
        action = "removed"
    else:
        _, target_metadata, _ = find_entry(args.index, args.target)
        require_current_schema(target_metadata)
        require_supported_entry_type(target_metadata, args.registry)
        validate_relation_objects([dict(relation)], args.index, args.layout)
        matching_unresolved = [
            item
            for item in unresolved
            if item["predicate"] == args.predicate
            and item["reference"].casefold()
            == (args.resolve_reference or "").strip().casefold()
        ]
        if args.resolve_reference and not matching_unresolved:
            raise InvalidRequest(
                "No matching unresolved relation exists for this predicate "
                "and reference"
            )
        existing = find_edge(relations, args.predicate, args.target)
        if existing is not None and not args.resolve_reference:
            raise Conflict("Relation already exists")
        if existing is None:
            definitions = load_relation_definitions(args.layout)
            definition = definitions.get(args.predicate, {})
            if definition.get("symmetric") is True:
                if (
                    find_edge(
                        metadata_list(target_metadata, "relations"),
                        args.predicate,
                        args.id,
                    )
                    is not None
                ):
                    raise Conflict(
                        "Symmetric relation is already stored in reverse"
                    )
            relations, relation = apply_relation_validity(
                relations,
                relation,
                valid_from=args.valid_from,
                valid_until=args.valid_until,
                supersedes=args.supersedes,
            )
            metadata["relations"] = [*relations, relation]
        elif args.valid_from or args.valid_until or args.supersedes:
            raise Conflict(
                "Relation already exists; validity is recorded when it is "
                "first asserted"
            )
        if args.resolve_reference:
            metadata["unresolved_relations"] = [
                item for item in unresolved if item not in matching_unresolved
            ]
            if not metadata["unresolved_relations"]:
                metadata.pop("unresolved_relations")
            action = "resolved"
        else:
            action = "added"

    metadata["updated_at"] = now_iso()
    atomic_write_entry(path, metadata, body)
    print(
        json.dumps(
            {
                "updated": str(path.relative_to(root)),
                "id": args.id,
                "relation": relation,
                "action": action,
                "resolved_reference": args.resolve_reference,
                # Both absent for an ordinary relation, which is almost every
                # one: a result must not carry a key per feature it did not use.
                **({"supersedes": args.supersedes} if args.supersedes else {}),
                **({"unlinked_supersessions": unlinked} if unlinked else {}),
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_tag(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)
    existing = metadata.get("tags")
    if not isinstance(existing, list) or not all(
        isinstance(tag, str) for tag in existing
    ):
        raise ValidationError("Entry tags are invalid; run validate")

    # Case folding decides whether two tags are the *same* tag; it must never
    # decide how a tag is *stored*. Folding the stored value rewrote every
    # unrelated tag on the entry as a side effect of touching one of them, and
    # `casefold` is lossy for several scripts ("Straße" became "strasse"), so
    # the original spelling could not be recovered afterwards.
    existing_by_key: dict[str, str] = {}
    for tag in existing:
        stripped = tag.strip()
        if stripped:
            existing_by_key.setdefault(tag_key(stripped), stripped)
    requested_by_key: dict[str, str] = {}
    for tag in args.tags:
        stripped = tag.strip()
        if stripped:
            requested_by_key.setdefault(tag_key(stripped), stripped)
    if not requested_by_key:
        raise InvalidRequest("At least one non-empty tag is required")

    if args.remove:
        changed_keys = existing_by_key.keys() & requested_by_key.keys()
        if not changed_keys:
            raise NoChange("None of the requested tags are present")
        # Report the spelling that was stored, not the one that was requested.
        changed = {existing_by_key[key] for key in changed_keys}
        result = {
            key: value
            for key, value in existing_by_key.items()
            if key not in changed_keys
        }
        action = "removed"
    else:
        changed_keys = requested_by_key.keys() - existing_by_key.keys()
        if not changed_keys:
            raise NoChange("All requested tags already exist")
        changed = {requested_by_key[key] for key in changed_keys}
        result = dict(existing_by_key)
        for key in changed_keys:
            result[key] = requested_by_key[key]
        action = "added"

    metadata["tags"] = sorted(result.values())
    metadata["updated_at"] = now_iso()
    atomic_write_entry(path, metadata, body)
    print(
        json.dumps(
            {
                "updated": str(path.relative_to(root)),
                "id": args.id,
                "tags": sorted(changed),
                "action": action,
                "revision": revision_for(path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_status(args: argparse.Namespace, root: Path) -> int:
    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_supported_entry_type(metadata, args.registry)
    require_expected_revision(path, args.expected_revision)
    if args.lifecycle is None and args.reading is None and args.interest is None:
        raise InvalidRequest("Specify --lifecycle, --reading, and/or --interest")
    changed: dict[str, object] = {}
    if args.lifecycle is not None and args.lifecycle != metadata.get("status"):
        metadata["status"] = args.lifecycle
        changed["status"] = args.lifecycle
    if args.reading is not None:
        if metadata.get("type") != "bookmark":
            raise InvalidRequest("--reading is only valid for bookmarks")
        if args.reading != metadata.get("reading_status"):
            metadata["reading_status"] = args.reading
            changed["reading_status"] = args.reading
            if args.reading == "read":
                read_at = now_iso()
                metadata["read_at"] = read_at
                changed["read_at"] = read_at
            elif "read_at" in metadata:
                metadata.pop("read_at")
                changed["read_at"] = None
    if args.interest is not None:
        if metadata.get("type") not in INTEREST_TYPES:
            raise InvalidRequest(
                "--interest is only valid for places, products, and recipes"
            )
        if args.interest != metadata.get("interest_status"):
            metadata["interest_status"] = args.interest
            changed["interest_status"] = args.interest
    if not changed:
        raise NoChange("Status would not change the entry")

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
        )
    )
    return 0


def command_retype(args: argparse.Namespace, root: Path) -> int:
    """Correct the type of an entry that was filed under the wrong one.

    Every type added after a vault exists leaves entries behind under the type
    they were given. Recipes captured before the `recipe` type existed stayed
    notes, and a search for `type: recipe` then reports nothing while the
    entries sit in plain sight. `update` deliberately refuses to touch `type`,
    because the type decides both the storage path and which attributes are
    legal -- so the correction needs an operation that moves the file and
    revalidates the attributes rather than a field a caller can set.

    Previews by default, like `migrate`, `import` and `purge`: retyping is a
    structural change to personal content, so the plan is shown first and
    `--apply` writes it.
    """

    path, metadata, body = find_entry(args.index, args.id)
    require_current_schema(metadata)
    require_expected_revision(path, args.expected_revision)

    current_type = entry_type_name(metadata)
    if current_type is None:
        raise ValidationError("Entry has no valid type")
    if current_type == args.to:
        raise NoChange(f"Entry is already of type {args.to!r}")

    try:
        definition = args.registry.require_type(args.to)
    except SchemaPackError as exc:
        raise InvalidRequest(str(exc)) from exc

    # Attributes are per type, so only what the target declares can survive.
    # Dropping the rest silently would lose personal content, so it is refused
    # unless the caller says the loss is intended.
    attributes = metadata.get("attributes")
    attributes = dict(attributes) if isinstance(attributes, dict) else {}
    kept = {
        name: value
        for name, value in attributes.items()
        if name in definition.fields
    }
    dropped = sorted(set(attributes) - set(kept))
    if dropped and not args.drop_unsupported:
        raise InvalidRequest(
            f"Type {args.to!r} does not define "
            + ", ".join(dropped)
            + ". Re-run with --drop-unsupported to discard "
            + ("them" if len(dropped) > 1 else "it")
            + ", after copying anything worth keeping into the body."
        )

    try:
        resolved = definition.validate_attributes(kept, apply_defaults=True)
    except SchemaPackError as exc:
        raise InvalidRequest(
            f"Entry cannot become {args.to!r}: {exc}"
        ) from exc
    defaulted = sorted(set(resolved) - set(kept))

    if args.to in ENTRY_TYPES:
        destination_directory = entry_directory(root, args.to)
    else:
        destination_directory = prepare_custom_entry_directory(
            root, args.to, args.registry
        )
    # The filename keeps the entry's own creation date and stable ID, so the
    # move changes the directory and nothing a reader uses to recognise it.
    created = metadata.get("created_at")
    day = str(created)[:10] if isinstance(created, str) else now_iso()[:10]
    entry_id = str(metadata.get("id", ""))
    title = str(metadata.get("title", ""))
    destination = destination_directory / (
        f"{day}--{slugify(title)}--{entry_id[3:11]}.md"
    )

    plan: dict[str, object] = {
        "dry_run": not args.apply,
        "id": entry_id,
        "from": {"type": current_type, "path": path.relative_to(root).as_posix()},
        "to": {
            "type": args.to,
            "type_version": definition.version,
            "path": destination.relative_to(root).as_posix(),
        },
        "attributes": {
            "kept": sorted(kept),
            "dropped": dropped,
            "defaulted": defaulted,
        },
    }

    if destination.exists() and destination != path:
        raise Conflict(
            "Target path already exists; no files were changed: "
            f"{destination.relative_to(root)}"
        )

    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    metadata["type"] = args.to
    metadata["type_version"] = definition.version
    metadata["attributes"] = resolved
    metadata["updated_at"] = now_iso()
    relocate_entry(path, destination, metadata, body)
    plan["revision"] = revision_for(destination)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0

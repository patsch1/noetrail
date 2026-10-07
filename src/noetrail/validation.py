#!/usr/bin/env python3
"""The `validate` command's rule set for one stored entry.

Everything type-specific is read from the registry. Before this module existed
the per-type rules were a ladder of `if entry_type == "place": ...` branches
that restated, in Python, what `knowledge-core/pack.yaml` already declared --
the same enum values, the same bounds, the same lengths, written twice and
kept in step by a parity test that only compared the enums. A required flag or
an upper bound could therefore drift between the two without anything
noticing.

What is left here is what the pack does not describe: the envelope every entry
carries regardless of its type (id, schema version, status, the trash fields,
relations, attachments) and the evaluation of the cross-field clauses --
`requires`, `host_of`, `normalized` -- that the pack *declares* but, being a
data format, cannot execute.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from noetrail.constants import (
    ACTIVE_STATUSES,
    ATTACHMENT_ID_PATTERN,
    ATTACHMENT_MEDIA_TYPES,
    ATTACHMENT_ORIGINS,
    ATTACHMENT_PATH_PATTERN,
    ATTACHMENT_SHA256_PATTERN,
    ATTRIBUTE_ENVELOPE_VERSIONS,
    COMPATIBLE_SCHEMA_VERSIONS,
    CUSTOM_ENTRY_FIELDS,
    ENTRY_TYPES,
    ID_PATTERN,
    LEGACY_EXPERIENCE_TYPES,
    MAX_ALIAS_CHARS,
    MAX_ALIASES_PER_ENTRY,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_ENTRY,
    REQUIRED_FIELDS,
    SENSITIVITIES,
    STATUSES,
)
from noetrail.frontmatter import entry_type_name
from noetrail.migrations import get_schema_version
from noetrail.normalize import canonicalize_url, name_key
from noetrail.provenance import provenance_errors
from noetrail.schema import (
    SchemaPackError,
    SchemaRegistry,
    TypeDefinition,
)
from noetrail.temporal import (
    RELATION_CORE_FIELDS,
    RELATION_FIELDS,
    RELATION_INSTANT_FIELDS,
    interval_errors,
    supersession_errors,
)

TRASH_FIELDS = {
    "previous_status",
    "deleted_at",
    "deleted_from",
    "purge_after",
}
REQUIRED_ATTACHMENT_FIELDS = {
    "id",
    "path",
    "media_type",
    "sha256",
    "size_bytes",
    "original_name",
    "added_at",
    "origin",
}
OPTIONAL_ATTACHMENT_FIELDS = {"caption", "experienced_at"}


def timestamp_errors(value: object, label: str) -> list[str]:
    """The ISO 8601 rule the envelope applies to its own timestamps."""

    if not isinstance(value, str):
        return [f"{label} must be an ISO 8601 timestamp"]
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return [f"{label} must be an ISO 8601 timestamp"]
    if parsed.tzinfo is None:
        return [f"{label} must include a timezone"]
    return []


def builtin_domain_fields(registry: SchemaRegistry) -> set[str]:
    """Every field name some built-in type declares.

    A field from this set on an entry whose type does not declare it is the
    generalisation of the hand-written "non-recipe entry contains recipe
    fields" family of checks: adding a field to a pack extends the rule.
    """

    names: set[str] = set()
    for definition in registry.builtin_types.values():
        names.update(definition.fields)
    return names


def cross_condition_errors(
    definition: TypeDefinition,
    values: dict[str, object],
) -> list[str]:
    """Evaluate the pack's cross-field clauses against one entry's values.

    The pack states the relations; this is the only place that acts on them,
    which is what keeps `pack.yaml` declarative rather than a small language
    with an interpreter hidden in the loader.
    """

    errors: list[str] = []
    for name, field in sorted(definition.fields.items()):
        if name not in values:
            continue
        value = values[name]
        for sibling, expected in field.requires:
            if expected is None:
                if sibling not in values:
                    errors.append(f"{name} requires {sibling}")
            elif values.get(sibling) != expected:
                errors.append(f"{name} requires {sibling} {expected}")
        if field.normalized and isinstance(value, str):
            try:
                canonical = canonicalize_url(value)
            except ValueError:
                errors.append(f"invalid {name}")
            else:
                if canonical != value:
                    errors.append(f"{name} is not normalized")
        if field.host_of is not None:
            target = values.get(field.host_of)
            if isinstance(target, str) and urlsplit(target).hostname != value:
                errors.append(f"{name} does not match {field.host_of}")
    return errors


def custom_type_errors(
    path: Path,
    metadata: dict[str, object],
    definition: TypeDefinition,
    *,
    in_trash: bool,
) -> list[str]:
    errors: list[str] = []
    type_version = metadata.get("type_version")
    if (
        isinstance(type_version, bool)
        or not isinstance(type_version, int)
        or type_version != definition.version
    ):
        errors.append(
            f"type_version must equal installed version "
            f"{definition.version} for {definition.qualified_id}"
        )
    try:
        attributes = definition.validate_attributes(
            metadata.get("attributes"),
            apply_defaults=False,
        )
    except SchemaPackError as exc:
        errors.append(str(exc))
        attributes = {}
    errors.extend(cross_condition_errors(definition, attributes))
    unexpected = sorted(set(metadata) - CUSTOM_ENTRY_FIELDS)
    if unexpected:
        errors.append(
            "pack-defined entry contains unsupported top-level fields: "
            f"{', '.join(unexpected)}"
        )
    expected_parent = (
        Path("vault") / "custom" / definition.pack_id / definition.local_id
    )
    if in_trash:
        deleted_from = metadata.get("deleted_from")
        if (
            isinstance(deleted_from, str)
            and Path(deleted_from).parent != expected_parent
        ):
            errors.append(
                "deleted_from does not match the derived custom type path"
            )
    elif path.parent != expected_parent:
        errors.append(
            f"entry is outside the derived custom type path {expected_parent}"
        )
    return errors


def builtin_type_errors(
    metadata: dict[str, object],
    definition: TypeDefinition,
    registry: SchemaRegistry,
    schema_version: int | None,
) -> tuple[list[str], dict[str, object]]:
    """Check the domain half of a built-in entry against its pack definition.

    Schema 9 and 10 keep those fields under `attributes`; schema 8 keeps them
    at the top level. Both are handed to the same `validate_attributes`, so
    required flags, enums, bounds, and lengths come from one source for both
    layouts and for pack-defined types alike.
    """

    errors: list[str] = []
    entry_type = definition.local_id
    supplied: object
    if schema_version in ATTRIBUTE_ENVELOPE_VERSIONS:
        type_version = metadata.get("type_version")
        if (
            isinstance(type_version, bool)
            or not isinstance(type_version, int)
            or type_version != definition.version
        ):
            errors.append(
                f"type_version must equal installed version "
                f"{definition.version} for {entry_type}"
            )
        supplied = metadata.get("attributes")
        misplaced = sorted(set(definition.fields) & metadata.keys())
        if misplaced:
            errors.append(
                "schema 9 built-in entry has domain fields outside "
                f"attributes: {', '.join(misplaced)}"
            )
    else:
        unexpected = sorted({"type_version", "attributes"} & metadata.keys())
        if unexpected:
            errors.append(
                "schema 8 built-in entry contains schema 9 type fields: "
                f"{', '.join(unexpected)}"
            )
        supplied = {
            name: metadata[name] for name in definition.fields if name in metadata
        }
    try:
        values = definition.validate_attributes(supplied, apply_defaults=False)
    except SchemaPackError as exc:
        errors.append(str(exc))
        values = {}
    errors.extend(cross_condition_errors(definition, values))
    stray = sorted(
        (builtin_domain_fields(registry) - set(definition.fields))
        & metadata.keys()
    )
    if stray:
        errors.append(
            f"entry contains fields {entry_type} does not declare: "
            f"{', '.join(stray)}"
        )
    return errors, values


def trash_errors(metadata: dict[str, object], *, in_trash: bool) -> list[str]:
    errors: list[str] = []
    if not in_trash:
        if metadata.get("status") == "trashed":
            errors.append("trashed entries must live below trash/")
        unexpected = sorted((TRASH_FIELDS | {"deletion_reason"}) & metadata.keys())
        if unexpected:
            errors.append(
                f"active entry contains trash fields: {', '.join(unexpected)}"
            )
        return errors
    if metadata.get("status") != "trashed":
        errors.append("file below trash/ must have status trashed")
    missing = sorted(TRASH_FIELDS - metadata.keys())
    if missing:
        errors.append(f"missing trash fields: {', '.join(missing)}")
    if metadata.get("previous_status") not in ACTIVE_STATUSES:
        errors.append("invalid previous_status")
    for field in ("deleted_at", "purge_after"):
        errors.extend(timestamp_errors(metadata.get(field), field))
    deleted_from = metadata.get("deleted_from")
    if not isinstance(deleted_from, str):
        errors.append("deleted_from must be a relative path below vault/")
    else:
        relative = Path(deleted_from)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] != "vault"
        ):
            errors.append("deleted_from must be a relative path below vault/")
    return errors


def attachment_errors(metadata: dict[str, object]) -> list[str]:
    attachments = metadata.get("attachments", [])
    if not isinstance(attachments, list):
        return ["attachments must be an array"]
    errors: list[str] = []
    if len(attachments) > MAX_ATTACHMENTS_PER_ENTRY:
        errors.append(
            f"attachments must contain at most {MAX_ATTACHMENTS_PER_ENTRY} items"
        )
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for index, attachment in enumerate(attachments):
        prefix = f"attachment {index}"
        if not isinstance(attachment, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = sorted(REQUIRED_ATTACHMENT_FIELDS - attachment.keys())
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
        unexpected = sorted(
            set(attachment)
            - REQUIRED_ATTACHMENT_FIELDS
            - OPTIONAL_ATTACHMENT_FIELDS
        )
        if unexpected:
            errors.append(f"{prefix} has invalid fields: {', '.join(unexpected)}")

        attachment_id = attachment.get("id")
        if not isinstance(attachment_id, str) or not ATTACHMENT_ID_PATTERN.fullmatch(
            attachment_id
        ):
            errors.append(f"{prefix} has invalid id")
        elif attachment_id in seen_ids:
            errors.append(f"{prefix} has duplicated id")
        else:
            seen_ids.add(attachment_id)

        digest = attachment.get("sha256")
        if not isinstance(digest, str) or not ATTACHMENT_SHA256_PATTERN.fullmatch(
            digest
        ):
            errors.append(f"{prefix} has invalid sha256")
        elif digest in seen_hashes:
            errors.append(f"{prefix} duplicates an attached image")
        else:
            seen_hashes.add(digest)

        attachment_path = attachment.get("path")
        if not isinstance(
            attachment_path, str
        ) or not ATTACHMENT_PATH_PATTERN.fullmatch(attachment_path):
            errors.append(f"{prefix} has invalid path")
        elif isinstance(digest, str) and not Path(attachment_path).name.startswith(
            f"{digest}."
        ):
            errors.append(f"{prefix} path does not match sha256")

        media_type = attachment.get("media_type")
        if not isinstance(media_type, str) or media_type not in ATTACHMENT_MEDIA_TYPES:
            errors.append(f"{prefix} has invalid media_type")
        elif isinstance(attachment_path, str) and not attachment_path.endswith(
            f".{ATTACHMENT_MEDIA_TYPES[media_type]}"
        ):
            errors.append(f"{prefix} path does not match media_type")

        size_bytes = attachment.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 1
            or size_bytes > MAX_ATTACHMENT_BYTES
        ):
            errors.append(f"{prefix} has invalid size_bytes")

        original_name = attachment.get("original_name")
        if (
            not isinstance(original_name, str)
            or not original_name.strip()
            or len(original_name) > 255
            or Path(original_name).name != original_name
        ):
            errors.append(f"{prefix} has invalid original_name")
        if attachment.get("origin") not in ATTACHMENT_ORIGINS:
            errors.append(f"{prefix} has invalid origin")

        for field in ("added_at", "experienced_at"):
            if field in attachment:
                errors.extend(
                    timestamp_errors(attachment.get(field), f"{prefix} {field}")
                )
        caption = attachment.get("caption")
        if caption is not None and (
            not isinstance(caption, str)
            or not caption.strip()
            or len(caption) > 2_000
        ):
            errors.append(f"{prefix} has invalid caption")
        if "experienced_at" in attachment:
            if metadata.get("type") not in LEGACY_EXPERIENCE_TYPES:
                errors.append(
                    f"{prefix} experienced_at is only valid for places and products"
                )
            if "last_experienced_at" not in metadata:
                errors.append(
                    f"{prefix} experienced_at requires a recorded experience"
                )
    return errors


def relation_errors(
    metadata: dict[str, object],
    predicates: set[str],
    schema_version: int | None = None,
) -> list[str]:
    errors: list[str] = []
    relations = metadata.get("relations")
    if not isinstance(relations, list):
        errors.append("relations must be an array")
    else:
        for index, relation in enumerate(relations):
            if not isinstance(relation, dict):
                errors.append(f"relation {index} must be an object")
                continue
            keys = set(relation)
            if not RELATION_CORE_FIELDS <= keys or not keys <= RELATION_FIELDS:
                errors.append(f"relation {index} has invalid fields")
                continue
            if relation.get("predicate") not in predicates:
                errors.append(f"relation {index} has unknown predicate")
            if not isinstance(relation.get("target"), str):
                errors.append(f"relation {index} has invalid target")
            temporal = keys - RELATION_CORE_FIELDS
            if temporal and schema_version is not None and schema_version < 11:
                errors.append(
                    f"relation {index} validity requires schema_version 11, "
                    f"not {schema_version}"
                )
            for field in RELATION_INSTANT_FIELDS:
                if field in relation:
                    errors.extend(
                        timestamp_errors(
                            relation.get(field), f"relation {index} {field}"
                        )
                    )
            errors.extend(interval_errors(relation, index))
        errors.extend(supersession_errors(relations))
    unresolved = metadata.get("unresolved_relations", [])
    if not isinstance(unresolved, list):
        errors.append("unresolved_relations must be an array")
        return errors
    seen: list[dict[str, object]] = []
    for index, relation in enumerate(unresolved):
        if not isinstance(relation, dict):
            errors.append(f"unresolved relation {index} must be an object")
            continue
        if set(relation) != {"predicate", "reference"}:
            errors.append(f"unresolved relation {index} has invalid fields")
            continue
        if relation.get("predicate") not in predicates:
            errors.append(f"unresolved relation {index} has unknown predicate")
        reference = relation.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            errors.append(f"unresolved relation {index} has invalid reference")
        if relation in seen:
            errors.append(f"unresolved relation {index} is duplicated")
        seen.append(relation)
    return errors


def validate_metadata(
    path: Path,
    metadata: dict[str, object],
    predicates: set[str],
    registry: SchemaRegistry,
    *,
    in_trash: bool,
) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - metadata.keys())
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    if not isinstance(metadata.get("id"), str) or not ID_PATTERN.fullmatch(
        str(metadata.get("id", ""))
    ):
        errors.append("invalid id")
    schema_version: int | None = None
    if "schema_version" in metadata:
        try:
            schema_version = get_schema_version(metadata)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if schema_version not in COMPATIBLE_SCHEMA_VERSIONS:
                errors.append(
                    f"unsupported schema_version {schema_version}; "
                    f"expected one of {sorted(COMPATIBLE_SCHEMA_VERSIONS)}"
                )
    # A non-string `type` is exactly what `validate` exists to report, so it
    # must not be used as a registry key first: a list or object there is
    # unhashable and used to abort the whole run with a TypeError.
    entry_type = entry_type_name(metadata)
    custom_type = (
        registry.custom_types.get(entry_type) if entry_type is not None else None
    )
    builtin_type = (
        registry.builtin_types.get(entry_type) if entry_type is not None else None
    )
    if entry_type not in ENTRY_TYPES and custom_type is None:
        errors.append("invalid type")

    domain: dict[str, object] = {}
    if custom_type is not None:
        errors.extend(
            custom_type_errors(path, metadata, custom_type, in_trash=in_trash)
        )
    elif entry_type in ENTRY_TYPES:
        if builtin_type is None:
            errors.append("built-in entry has no installed type definition")
        else:
            type_errors, domain = builtin_type_errors(
                metadata,
                builtin_type,
                registry,
                schema_version,
            )
            errors.extend(type_errors)
    # The domain values a schema 9 entry keeps under `attributes` are what the
    # envelope rules below have to see: an attachment's `experienced_at` is
    # allowed by `last_experienced_at`, wherever that field is stored.
    effective: dict[str, object] = {**metadata, **domain}

    title_value = metadata.get("title")
    if not isinstance(title_value, str) or not title_value.strip():
        errors.append("invalid title")
    aliases = metadata.get("aliases", [])
    if aliases and schema_version is not None and schema_version < 12:
        errors.append(
            f"aliases require schema_version 12, not {schema_version}"
        )
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) for alias in aliases
    ):
        errors.append("aliases must be an array of strings")
    else:
        if len(aliases) > MAX_ALIASES_PER_ENTRY:
            errors.append(
                f"aliases must contain at most {MAX_ALIASES_PER_ENTRY} values"
            )
        seen_aliases: set[str] = set()
        title_identity = name_key(title_value) if isinstance(title_value, str) else ""
        for alias in aliases:
            identity = name_key(alias)
            if not alias.strip() or len(alias.strip()) > MAX_ALIAS_CHARS:
                errors.append(f"invalid alias: {alias!r}")
            elif identity == title_identity:
                errors.append("alias duplicates title")
            elif identity in seen_aliases:
                errors.append("aliases contain duplicates")
            seen_aliases.add(identity)
    if metadata.get("status") not in STATUSES:
        errors.append("invalid status")
    errors.extend(trash_errors(metadata, in_trash=in_trash))
    if metadata.get("sensitivity") not in SENSITIVITIES:
        errors.append("invalid sensitivity")
    tags = metadata.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        errors.append("tags must be an array of strings")
    if "provenance" in metadata:
        if schema_version is not None and schema_version < 10:
            errors.append(
                f"provenance requires schema_version 10, not {schema_version}"
            )
        errors.extend(provenance_errors(metadata.get("provenance"), metadata))
    errors.extend(attachment_errors(effective))
    errors.extend(relation_errors(metadata, predicates, schema_version))
    if "reviewed_at" in metadata:
        errors.extend(timestamp_errors(metadata.get("reviewed_at"), "reviewed_at"))
    return [f"{path}: {error}" for error in errors]

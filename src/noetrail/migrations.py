"""Ordered, dependency-free migrations for knowledge entry frontmatter."""

from __future__ import annotations

from collections.abc import Callable, Mapping

CURRENT_SCHEMA_VERSION = 12

ORIGINS = ("web", "user", "agent")
"""Values the per-field ``provenance`` map may carry.

``web`` means the value was taken from a fetched page, ``user`` means a human
supplied it, ``agent`` means a model produced it from something other than a
fetch. The map records where a value came from; it does not decide what a
reader is allowed to do with it.
"""

WEB_METADATA_FIELDS: tuple[str, ...] = (
    "title",
    "site_name",
    "published_at",
    "language",
    "page_description",
    "authors",
)
"""Bookmark fields a page can populate with free text.

These are the ones the fetcher fills from ``<meta>`` and ``<title>``, so these
are the ones an attacker controls. ``url``, ``canonical_url``, ``domain`` and
the enum fields are excluded: they are either supplied by the user or checked
against a closed value set before they are written.
"""

FETCHED_STATUSES = frozenset({"complete", "partial"})

BuiltinTypeLayouts = Mapping[
    str, tuple[int, tuple[str, ...], Mapping[str, object]]
]
"""Per built-in type: its version, its field names, and its field defaults.

The defaults matter during 8->9. Fields that became required *after* the
entry was written do not exist on it, and moving the metadata without
filling them produced an entry that validated cleanly before the migration
and not afterwards -- with no rollback and no way to edit it back into
shape, because every write command refuses an entry it cannot validate.
"""

Migration = Callable[
    [dict[str, object], str],
    tuple[dict[str, object], str],
]


def _with_schema_version(
    metadata: dict[str, object], version: int
) -> dict[str, object]:
    result: dict[str, object] = {}
    inserted = False
    for key, value in metadata.items():
        if key == "schema_version":
            continue
        result[key] = value
        if key == "id":
            result["schema_version"] = version
            inserted = True
    if not inserted:
        result = {"schema_version": version, **result}
    return result


def migrate_0_to_1(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Mark previously unversioned entries as schema version 1."""
    return _with_schema_version(metadata, 1), body


def migrate_1_to_2(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable the trash lifecycle without changing semantic entry data."""
    return _with_schema_version(metadata, 2), body


def migrate_2_to_3(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable review metadata without changing existing review decisions."""
    return _with_schema_version(metadata, 3), body


def migrate_3_to_4(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Add neutral bookmark classification without inventing reading dates."""
    result = _with_schema_version(metadata, 4)
    if result.get("type") != "bookmark" or "bookmark_kind" in result:
        return result, body

    ordered: dict[str, object] = {}
    inserted = False
    for key, value in result.items():
        ordered[key] = value
        if key == "reading_status":
            ordered["bookmark_kind"] = "unknown"
            inserted = True
    if not inserted:
        ordered["bookmark_kind"] = "unknown"
    return ordered, body


def migrate_4_to_5(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable place and product entries without changing existing semantics."""
    return _with_schema_version(metadata, 5), body


def migrate_5_to_6(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable attachment references without inventing attachment data."""
    return _with_schema_version(metadata, 6), body


def migrate_6_to_7(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable first-class experiences without inventing historical events."""
    return _with_schema_version(metadata, 7), body


def migrate_7_to_8(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable recipes and cooking experiences without inventing either."""
    return _with_schema_version(metadata, 8), body


def migrate_8_to_9(
    metadata: dict[str, object],
    body: str,
    builtin_types: BuiltinTypeLayouts,
) -> tuple[dict[str, object], str]:
    """Move built-in domain fields below the registry-owned attributes map."""
    entry_type = metadata.get("type")
    if not isinstance(entry_type, str):
        raise ValueError("schema 8 entry has no valid type")

    # Custom pack entries already used the independently versioned attributes
    # envelope in schema 8. Their core migration is therefore only a schema
    # version bump; pack migrations remain a separate concern.
    if "/" in entry_type:
        if not isinstance(metadata.get("type_version"), int):
            raise ValueError(
                f"custom entry {entry_type!r} has no valid type_version"
            )
        if not isinstance(metadata.get("attributes"), dict):
            raise ValueError(
                f"custom entry {entry_type!r} has no attributes object"
            )
        return _with_schema_version(metadata, 9), body

    layout = builtin_types.get(entry_type)
    if layout is None:
        raise ValueError(
            f"no installed built-in type definition for {entry_type!r}"
        )
    if "type_version" in metadata or "attributes" in metadata:
        raise ValueError(
            "schema 8 built-in entry already contains schema 9 type fields"
        )

    type_version, field_names, defaults = layout
    domain_fields = set(field_names)
    attributes = {
        key: value for key, value in metadata.items() if key in domain_fields
    }
    for field, default in defaults.items():
        if field not in attributes:
            attributes[field] = default
    result: dict[str, object] = {}
    inserted = False
    for key, value in metadata.items():
        if key == "schema_version":
            result[key] = 9
            continue
        if key in domain_fields:
            continue
        result[key] = value
        if key == "type":
            result["type_version"] = type_version
            result["attributes"] = attributes
            inserted = True
    if not inserted:
        result["type_version"] = type_version
        result["attributes"] = attributes
    return result, body


def migrate_9_to_10(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Record which bookmark fields a fetch filled in, per field.

    Schema 9 kept no record of where a value came from. The fetcher already
    reported ``untrusted_web_metadata: true`` for its whole envelope, but that
    flag was dropped at the vault boundary, so a title or description carrying
    injection text was indistinguishable from one the user typed.

    Existing entries cannot say which of their fields a human later corrected,
    so the inference here is deliberately one-directional: a bookmark whose
    ``fetch_status`` says a fetch produced its metadata gets its free-text
    fields marked ``web``. Marking a hand-edited title as web overstates
    distrust; the opposite mistake would understate it. No field values and no
    body text are touched.
    """

    result = _with_schema_version(metadata, 10)
    if "provenance" in result:
        raise ValueError("schema 9 entry already contains a provenance map")
    if result.get("type") != "bookmark":
        return result, body

    attributes = result.get("attributes")
    if not isinstance(attributes, dict):
        raise ValueError("schema 9 bookmark has no attributes object")
    if attributes.get("fetch_status") not in FETCHED_STATUSES:
        return result, body

    provenance = {
        field: "web"
        for field in WEB_METADATA_FIELDS
        if field in attributes or field in result
    }
    if not provenance:
        return result, body

    ordered: dict[str, object] = {}
    inserted = False
    for key, value in result.items():
        ordered[key] = value
        if key == "attributes":
            ordered["provenance"] = provenance
            inserted = True
    if not inserted:
        ordered["provenance"] = provenance
    return ordered, body


def migrate_10_to_11(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Allow a relation to carry a validity interval, without inventing one.

    Schema 11 widens the relation object: besides ``predicate`` and ``target``
    it may now carry ``valid_from``, ``valid_until``, ``superseded_by`` and
    ``recorded_at``. The widening is what the migration enables; it deliberately
    fills nothing in.

    Deriving an interval from ``created_at`` was the obvious temptation and is
    the one thing this must not do. ``created_at`` says when the vault learned
    the entry, not since when the statement held in the world -- those are the
    two clocks this schema exists to keep apart, and seeding one from the other
    would write a claim about the world that nobody made and that no later
    reader could tell from a real one. An absent interval means "not recorded",
    which is exactly the state every existing relation is in.
    """

    return _with_schema_version(metadata, 11), body


def migrate_11_to_12(
    metadata: dict[str, object], body: str
) -> tuple[dict[str, object], str]:
    """Enable optional aliases without inventing alternative names."""

    return _with_schema_version(metadata, 12), body


MIGRATIONS: dict[int, tuple[str, Migration]] = {
    0: ("add-schema-version", migrate_0_to_1),
    1: ("add-trash-lifecycle", migrate_1_to_2),
    2: ("add-review-workflow", migrate_2_to_3),
    3: ("add-bookmark-reading-metadata", migrate_3_to_4),
    4: ("add-places-products-experiences", migrate_4_to_5),
    5: ("add-attachment-references", migrate_5_to_6),
    6: ("add-structured-experiences", migrate_6_to_7),
    7: ("add-recipes-and-cooking", migrate_7_to_8),
    9: ("add-field-provenance", migrate_9_to_10),
    10: ("add-relation-validity", migrate_10_to_11),
    11: ("add-entry-aliases", migrate_11_to_12),
}


def get_schema_version(metadata: dict[str, object]) -> int:
    value = metadata.get("schema_version", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("schema_version must be an integer")
    if value < 0:
        raise ValueError("schema_version cannot be negative")
    return value


def migrate_entry(
    metadata: dict[str, object],
    body: str,
    *,
    builtin_types: BuiltinTypeLayouts | None = None,
) -> tuple[dict[str, object], str, int, list[str]]:
    start_version = get_schema_version(metadata)
    if start_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"entry uses future schema version {start_version}; "
            f"this tool supports {CURRENT_SCHEMA_VERSION}"
        )

    migrated_metadata = dict(metadata)
    migrated_body = body
    version = start_version
    steps: list[str] = []
    while version < CURRENT_SCHEMA_VERSION:
        if version == 8:
            if builtin_types is None:
                raise ValueError(
                    "migration from schema 8 requires the active built-in "
                    "type registry"
                )
            migrated_metadata, migrated_body = migrate_8_to_9(
                migrated_metadata,
                migrated_body,
                builtin_types,
            )
            next_version = get_schema_version(migrated_metadata)
            if next_version != 9:
                raise ValueError(
                    "migration move-builtins-to-attributes produced schema "
                    f"version {next_version}; expected 9"
                )
            steps.append("8->9:move-builtins-to-attributes")
            version = next_version
            continue
        migration_info = MIGRATIONS.get(version)
        if migration_info is None:
            raise ValueError(f"no migration exists from schema version {version}")
        name, migration = migration_info
        migrated_metadata, migrated_body = migration(
            migrated_metadata, migrated_body
        )
        next_version = get_schema_version(migrated_metadata)
        if next_version != version + 1:
            raise ValueError(
                f"migration {name} produced schema version {next_version}; "
                f"expected {version + 1}"
            )
        steps.append(f"{version}->{next_version}:{name}")
        version = next_version

    return migrated_metadata, migrated_body, start_version, steps

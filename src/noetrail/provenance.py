#!/usr/bin/env python3
"""Per-field origin records and the fence around text that came off the web."""

from __future__ import annotations

from noetrail.body import body_has_web_content, body_sections
from noetrail.constants import MAX_PROVENANCE_FIELDS, PROVENANCE_FIELD_PATTERN
from noetrail.errors import (
    InvalidRequest,
)
from noetrail.migrations import (
    FETCHED_STATUSES,
    ORIGINS,
    WEB_METADATA_FIELDS,
)


def entry_provenance(metadata: dict[str, object]) -> dict[str, str]:
    """Return the persisted per-field origin map of one entry.

    Fields that carry an unknown origin are dropped rather than reported: a
    caller asking "which of these values came off the network" must not be
    handed a value it cannot interpret. `validate` is what reports the damage.
    """

    value = metadata.get("provenance")
    if not isinstance(value, dict):
        return {}
    return {
        field: origin
        for field, origin in value.items()
        if isinstance(field, str) and origin in ORIGINS
    }


def fields_with_origin(
    metadata: dict[str, object], origin: str = "web"
) -> list[str]:
    return sorted(
        field
        for field, value in entry_provenance(metadata).items()
        if value == origin
    )


def provenance_report(
    metadata: dict[str, object],
    body: str,
    headings: dict[str, str] | None = None,
) -> dict[str, object]:
    """Return the full origin picture of one entry, in its own object.

    Kept out of the flat result fields on purpose: a client that has never
    heard of provenance keeps working, and one that has can map every value it
    was handed to an origin without parsing the Markdown body. The values
    themselves are not repeated here -- doing so would double a bookmark
    summary against `MAX_RESULT_CHARS` and create a second copy of exactly the
    text this is about.
    """

    fields = entry_provenance(metadata)
    sections = body_sections(body, headings)
    web_body = any(section["origin"] == "web" for section in sections)
    web_fields = sorted(
        field for field, origin in fields.items() if origin == "web"
    )
    return {
        "fields": fields,
        "web_fields": web_fields,
        "web_body": web_body,
        "body_sections": sections,
        "has_web_content": bool(web_fields) or web_body,
    }


def search_provenance(
    metadata: dict[str, object],
    body: str,
    headings: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """Return the compact origin summary for one search hit, or None.

    None for every entry with no web-derived content, which is the normal case
    and keeps the result page from growing a key per item for nothing.
    """

    web_fields = fields_with_origin(metadata)
    web_body = body_has_web_content(body, headings)
    if not web_fields and not web_body:
        return None
    return {"web_fields": web_fields, "web_body": web_body}


def with_provenance(
    metadata: dict[str, object], provenance: dict[str, str]
) -> dict[str, object]:
    """Place the provenance map next to `attributes` in the envelope."""

    result: dict[str, object] = {}
    inserted = False
    for key, value in metadata.items():
        if key == "provenance":
            continue
        result[key] = value
        if key == "attributes" and provenance:
            result["provenance"] = provenance
            inserted = True
    if provenance and not inserted:
        result["provenance"] = provenance
    return result


def drop_provenance(metadata: dict[str, object], *fields: str) -> None:
    """Forget the recorded origin of fields the user has just overwritten.

    Without this a title fetched from a page kept its `web` mark after the
    user replaced it by hand, which is the one direction of error this whole
    mechanism must not make: it would tell a client to distrust the user's own
    words.
    """

    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        return
    for name in fields:
        provenance.pop(name, None)
    if not provenance:
        metadata.pop("provenance", None)


def parse_provenance_payload(payload: dict[str, object]) -> dict[str, str]:
    """Read an explicitly declared per-field origin map out of a payload."""

    declared = payload.get("provenance")
    if declared is None:
        return {}
    if not isinstance(declared, dict):
        raise InvalidRequest("provenance must be an object of field: origin")
    explicit: dict[str, str] = {}
    for name, origin in declared.items():
        if not isinstance(name, str) or not PROVENANCE_FIELD_PATTERN.fullmatch(
            name
        ):
            raise InvalidRequest(f"Invalid provenance field name: {name!r}")
        if origin not in ORIGINS:
            raise InvalidRequest(
                f"Invalid provenance origin for {name}: {origin!r}; "
                f"expected one of {', '.join(sorted(ORIGINS))}"
            )
        explicit[name] = origin
    return explicit


def bookmark_provenance(payload: dict[str, object]) -> dict[str, str]:
    """Decide which stored bookmark fields came off the fetched page.

    Two sources, in this order: the fetcher's own `untrusted_web_metadata`
    flag (or a `fetch_status` that says a fetch produced the metadata), which
    marks the free-text fields the page controls, and an explicit `provenance`
    map that overrides individual fields -- so a user who typed the title
    themselves after a fetch is not told their own words are untrusted.
    """

    flag = payload.get("untrusted_web_metadata", False)
    if not isinstance(flag, bool):
        raise InvalidRequest("untrusted_web_metadata must be a boolean")
    fetched = flag or payload.get("fetch_status") in FETCHED_STATUSES

    provenance: dict[str, str] = {}
    if fetched:
        provenance = {
            name: "web"
            for name in WEB_METADATA_FIELDS
            if payload.get(name) not in (None, "", [])
        }
    provenance.update(parse_provenance_payload(payload))
    return dict(sorted(provenance.items()))


def provenance_errors(
    value: object, metadata: dict[str, object]
) -> list[str]:
    """Check the per-field origin map against the entry it belongs to.

    The existence check is the point: a mark left behind by a field that was
    later replaced or removed would keep claiming an origin for a value that
    is no longer stored, and a stale `web` mark on the user's own words is the
    failure this mechanism must not produce.
    """

    if not isinstance(value, dict):
        return ["provenance must be an object of field: origin"]
    errors: list[str] = []
    if len(value) > MAX_PROVENANCE_FIELDS:
        errors.append(
            f"provenance must name at most {MAX_PROVENANCE_FIELDS} fields"
        )
    attributes = metadata.get("attributes")
    known = set(metadata) | (
        set(attributes) if isinstance(attributes, dict) else set()
    )
    for name, origin in value.items():
        if not isinstance(
            name, str
        ) or not PROVENANCE_FIELD_PATTERN.fullmatch(name):
            errors.append(f"provenance has an invalid field name: {name!r}")
            continue
        if origin not in ORIGINS:
            errors.append(
                f"provenance origin for {name} must be one of "
                f"{', '.join(sorted(ORIGINS))}"
            )
        if name not in known:
            errors.append(
                f"provenance names a field the entry does not have: {name}"
            )
    return errors

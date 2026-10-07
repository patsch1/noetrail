#!/usr/bin/env python3
"""Relation predicates and the parsing of relation arguments."""

from __future__ import annotations

import json
import re

from noetrail.entries import VaultIndex
from noetrail.errors import (
    Conflict,
    InvalidRequest,
    NotFound,
)
from noetrail.layout import (
    NoetrailLayout,
)


def load_relation_definitions(
    layout: NoetrailLayout,
) -> dict[str, dict[str, object]]:
    path = layout.relation_types_path
    definitions: dict[str, dict[str, object]] = {}
    if not path.exists():
        return definitions
    in_relations = False
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "relations:":
            in_relations = True
            continue
        if in_relations:
            match = re.match(r"^  ([a-z][a-z0-9_]*):\s*$", line)
            if match:
                current = match.group(1)
                definitions[current] = {}
                continue
            property_match = re.match(
                r"^    ([a-z][a-z0-9_]*):\s*(.+?)\s*$", line
            )
            if current and property_match:
                key, raw_value = property_match.groups()
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError:
                    value = raw_value
                definitions[current][key] = value
    return definitions


def load_relation_types(layout: NoetrailLayout) -> set[str]:
    return set(load_relation_definitions(layout))


def validate_relation_objects(
    values: list[dict[str, object]], index: VaultIndex, layout: NoetrailLayout
) -> list[dict[str, str]]:
    available_ids = index.ids
    predicates = load_relation_types(layout)
    relations = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"predicate", "target"}:
            raise InvalidRequest("Each relation must contain only predicate and target")
        predicate = value.get("predicate")
        target = value.get("target")
        if not isinstance(predicate, str) or not isinstance(target, str):
            raise InvalidRequest("Relation predicate and target must be strings")
        if predicate not in predicates:
            raise InvalidRequest(f"Unknown relation predicate: {predicate}")
        if target not in available_ids:
            raise NotFound(f"Relation target does not exist: {target}")
        relations.append({"predicate": predicate, "target": target})
    return relations


def parse_relations(
    values: list[str], index: VaultIndex, layout: NoetrailLayout
) -> list[dict[str, str]]:
    # `list[dict[str, str]]` is not a `list[dict[str, object]]` (mutable
    # containers are invariant), so the element type is widened here rather
    # than at the call.
    parsed: list[dict[str, object]] = []
    for value in values:
        if ":" not in value:
            raise InvalidRequest(
                f"Invalid relation {value!r}; expected predicate:target-id"
            )
        predicate, target = value.split(":", 1)
        parsed.append({"predicate": predicate, "target": target})
    return validate_relation_objects(parsed, index, layout)


def validate_unresolved_relation_objects(
    values: list[object], layout: NoetrailLayout
) -> list[dict[str, str]]:
    predicates = load_relation_types(layout)
    unresolved: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"predicate", "reference"}:
            raise InvalidRequest(
                "Each unresolved relation must contain only predicate and reference"
            )
        predicate = value.get("predicate")
        reference = value.get("reference")
        if not isinstance(predicate, str) or predicate not in predicates:
            raise InvalidRequest(f"Unknown relation predicate: {predicate}")
        if not isinstance(reference, str) or not reference.strip():
            raise InvalidRequest("Unresolved relation reference must be non-empty")
        relation = {"predicate": predicate, "reference": reference.strip()}
        if relation in unresolved:
            raise Conflict("Duplicate unresolved relation")
        unresolved.append(relation)
    return unresolved


def parse_unresolved_relations(
    values: list[str], layout: NoetrailLayout
) -> list[dict[str, str]]:
    parsed: list[object] = []
    for value in values:
        if ":" not in value:
            raise InvalidRequest(
                f"Invalid unresolved relation {value!r}; "
                "expected predicate:free-text-reference"
            )
        predicate, reference = value.split(":", 1)
        parsed.append({"predicate": predicate, "reference": reference})
    return validate_unresolved_relation_objects(parsed, layout)

"""Load bounded, declarative saved search views from instance configuration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from noetrail.constants import (
    BOOKMARK_KINDS,
    ENTRY_TYPES,
    EXPERIENCE_KINDS,
    ID_PATTERN,
    INTEREST_STATUSES,
    PLACE_KINDS,
    PRODUCT_KINDS,
    READING_STATUSES,
    SEARCH_SORTS,
)
from noetrail.layout import NoetrailLayout
from noetrail.schema import SchemaPackError, SchemaRegistry, parse_restricted_yaml
from noetrail.search import RANK_HYBRID, SEARCH_RANKS

VIEW_FORMAT_VERSION = 1
MAX_VIEWS = 128
MAX_VIEWS_BYTES = 256 * 1024
VIEW_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PREDICATE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ROOT_KEYS = {"format_version", "views"}
VIEW_KEYS = {
    "title",
    "description",
    "query",
    "type",
    "attribute_filters",
    "domain",
    "reading_status",
    "bookmark_kind",
    "read_after",
    "read_before",
    "place_kind",
    "product_kind",
    "experience_kind",
    "interest_status",
    "related_id",
    "relation_predicate",
    "experienced",
    "min_rating",
    "occurred_after",
    "occurred_before",
    "as_of",
    "limit",
    "sort",
    "rank",
}


class ViewError(ValueError):
    """A saved-view file is absent from the trusted data-only subset."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ViewError(f"{label} must be a mapping")
    return value


def _string(
    values: dict[str, object],
    name: str,
    *,
    maximum: int,
    required: bool = False,
) -> str | None:
    value = values.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ViewError(f"{name} must be a non-empty string")
    stripped = value.strip()
    if len(stripped) > maximum:
        raise ViewError(f"{name} must not exceed {maximum} characters")
    return stripped


@dataclass(frozen=True)
class ViewDefinition:
    name: str
    title: str
    description: str | None
    search: dict[str, object]

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            **({"description": self.description} if self.description else {}),
            "search": self.search,
        }

    def cli_values(self) -> dict[str, object]:
        values = dict(self.search)
        attribute_filters = values.pop("attribute_filters", None)
        if isinstance(attribute_filters, dict):
            values["attribute_filter"] = [
                f"{name}="
                + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                for name, value in sorted(attribute_filters.items())
            ]
        return values


@dataclass(frozen=True)
class ViewRegistry:
    views: dict[str, ViewDefinition]

    @classmethod
    def load(
        cls,
        layout: NoetrailLayout,
        schema_registry: SchemaRegistry,
    ) -> ViewRegistry:
        path = layout.views_path
        if not path.is_file():
            return cls({})
        try:
            if path.stat().st_size > MAX_VIEWS_BYTES:
                raise ViewError(
                    f"saved views exceed {MAX_VIEWS_BYTES} bytes"
                )
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ViewError(f"could not read saved views: {exc}") from exc
        try:
            parsed = parse_restricted_yaml(text, path)
        except SchemaPackError as exc:
            raise ViewError(str(exc)) from exc
        root = _mapping(parsed, "saved views")
        unknown = sorted(set(root) - ROOT_KEYS)
        if unknown:
            raise ViewError(f"saved views contain unknown keys: {', '.join(unknown)}")
        if root.get("format_version") != VIEW_FORMAT_VERSION:
            raise ViewError(
                f"saved views format_version must be {VIEW_FORMAT_VERSION}"
            )
        raw_views = _mapping(root.get("views"), "views")
        if len(raw_views) > MAX_VIEWS:
            raise ViewError(f"views must contain at most {MAX_VIEWS} entries")
        definitions: dict[str, ViewDefinition] = {}
        for name, raw in sorted(raw_views.items()):
            definitions[name] = _parse_view(name, raw, schema_registry)
        return cls(definitions)

    def require(self, name: str) -> ViewDefinition:
        try:
            return self.views[name]
        except KeyError as exc:
            raise ViewError(f"Unknown saved view: {name}") from exc

    def summary(self) -> dict[str, object]:
        return {
            "count": len(self.views),
            "views": [view.summary() for view in self.views.values()],
        }


def _parse_view(
    name: str,
    raw: object,
    registry: SchemaRegistry,
) -> ViewDefinition:
    if VIEW_NAME_PATTERN.fullmatch(name) is None:
        raise ViewError(f"invalid saved view name: {name}")
    values = _mapping(raw, f"view {name}")
    unknown = sorted(set(values) - VIEW_KEYS)
    if unknown:
        raise ViewError(f"view {name} contains unknown keys: {', '.join(unknown)}")
    title = _string(values, "title", maximum=200, required=True)
    assert title is not None
    description = _string(values, "description", maximum=1_000)

    search: dict[str, object] = {"query": values.get("query", "")}
    query = search["query"]
    if not isinstance(query, str) or len(query) > 2_000:
        raise ViewError(
            f"view {name} query must be a string of at most 2000 characters"
        )

    selected_type = values.get("type")
    custom_type = None
    if selected_type is not None:
        if not isinstance(selected_type, str):
            raise ViewError(f"view {name} type must be a string")
        if selected_type not in ENTRY_TYPES:
            try:
                custom_type = registry.require_type(selected_type)
            except SchemaPackError as exc:
                raise ViewError(str(exc)) from exc
        search["type"] = selected_type

    attribute_filters = values.get("attribute_filters")
    if attribute_filters is not None:
        filters = _mapping(attribute_filters, f"view {name} attribute_filters")
        if custom_type is None:
            raise ViewError(
                f"view {name} attribute_filters require a pack-defined type"
            )
        for field_name, value in filters.items():
            field = custom_type.fields.get(field_name)
            if field is None:
                raise ViewError(
                    f"view {name} has unknown attribute filter {field_name}"
                )
            if not field.searchable:
                raise ViewError(
                    f"view {name} attribute {field_name} is not searchable"
                )
            try:
                field.validate_value(value, f"view {name}.{field_name}")
            except SchemaPackError as exc:
                raise ViewError(str(exc)) from exc
        search["attribute_filters"] = filters

    enums = {
        "reading_status": READING_STATUSES,
        "bookmark_kind": BOOKMARK_KINDS,
        "place_kind": PLACE_KINDS,
        "product_kind": PRODUCT_KINDS,
        "experience_kind": EXPERIENCE_KINDS,
        "interest_status": INTEREST_STATUSES,
        "sort": SEARCH_SORTS,
        "rank": set(SEARCH_RANKS),
    }
    for field_name, allowed in enums.items():
        if field_name in values:
            value = values[field_name]
            if not isinstance(value, str) or value not in allowed:
                raise ViewError(f"view {name} has invalid {field_name}")
            search[field_name] = value

    for field_name, maximum in (
        ("domain", 255),
        ("read_after", 100),
        ("read_before", 100),
        ("occurred_after", 100),
        ("occurred_before", 100),
        ("as_of", 100),
    ):
        if field_name in values:
            value = _string(values, field_name, maximum=maximum, required=True)
            assert value is not None
            search[field_name] = value
    if "related_id" in values:
        value = values["related_id"]
        if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
            raise ViewError(f"view {name} has invalid related_id")
        search["related_id"] = value
    if "relation_predicate" in values:
        value = values["relation_predicate"]
        if not isinstance(value, str) or PREDICATE_PATTERN.fullmatch(value) is None:
            raise ViewError(f"view {name} has invalid relation_predicate")
        search["relation_predicate"] = value
    if "experienced" in values:
        if not isinstance(values["experienced"], bool):
            raise ViewError(f"view {name} experienced must be a boolean")
        search["experienced"] = values["experienced"]
    if "min_rating" in values:
        rating = values["min_rating"]
        if (
            isinstance(rating, bool)
            or not isinstance(rating, int)
            or not 1 <= rating <= 5
        ):
            raise ViewError(f"view {name} min_rating must be from 1 to 5")
        search["min_rating"] = rating
    limit = values.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ViewError(f"view {name} limit must be from 1 to 50")
    search["limit"] = limit
    search.setdefault("sort", "auto")
    # A view that does not name a ranking mode gets the one an ordinary search
    # gets. Repeating the literal here is how the two silently drifted apart
    # when the default moved.
    search.setdefault("rank", RANK_HYBRID)
    return ViewDefinition(name, title, description, search)

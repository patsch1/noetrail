#!/usr/bin/env python3
"""Load and validate declarative knowledge schema packs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import functools
import json
import math
from pathlib import Path
import re
from urllib.parse import SplitResult, urlsplit

from noetrail.layout import NoetrailLayout

PACK_FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_MANIFEST_LINES = 4_096
MAX_LINE_CHARS = 8_192
MAX_PACKS = 128
MAX_TYPES_PER_PACK = 64
MAX_FIELDS_PER_TYPE = 128
MAX_DESCRIPTION_CHARS = 4_000
MAX_TEMPLATE_BYTES = 100_000
COMPONENT_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
NUMBER_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
SUPPORTED_FIELD_TYPES = {
    "string",
    "text",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
    "url",
    "enum",
    "array",
}
ARRAY_ITEM_TYPES = {
    "string",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
    "url",
}
PACK_KEYS = {
    "format_version",
    "id",
    "version",
    "title",
    "description",
    "types",
}
TYPE_KEYS = {
    "title",
    "description",
    "fields",
    "body_sections",
    "template",
    "entry_type",
}
FIELD_KEYS = {
    "type",
    "required",
    "searchable",
    "default",
    "minimum",
    "maximum",
    "min_length",
    "max_length",
    "values",
    "items",
    "max_items",
    # Cross-field clauses. They state a relation between two fields of the
    # same type; nothing here is executable, and this module only checks that
    # what the pack declares is well formed and points at fields that exist.
    # `noetrail.validation` is what reads them when it checks an entry.
    "requires",
    "host_of",
    "normalized",
}
# `requires` values are compared against a stored value, so they have to be
# the kinds of literal the restricted YAML parser can produce.
REQUIRES_VALUE_TYPES = (str, int, float, bool)


class SchemaPackError(ValueError):
    """A pack is malformed, unsupported, duplicated, or unsafe."""


def _location(path: Path, line: int | None = None) -> str:
    return f"{path}:{line}" if line is not None else str(path)


def _parse_scalar(value: str, path: Path, line: int) -> object:
    if value in {"true", "false", "null"}:
        return json.loads(value)
    if NUMBER_PATTERN.fullmatch(value):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise SchemaPackError(
                f"{_location(path, line)}: invalid numeric value"
            ) from exc
    if value.startswith(('"', "[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise SchemaPackError(
                f"{_location(path, line)}: values must use JSON-compatible syntax"
            ) from exc
    if value.startswith(("&", "*", "!", "|", ">")) or value == "<<":
        raise SchemaPackError(
            f"{_location(path, line)}: YAML anchors, tags, aliases, merges, and "
            "block scalars are not supported"
        )
    if any(ord(character) < 32 for character in value):
        raise SchemaPackError(
            f"{_location(path, line)}: control characters are not supported"
        )
    return value


def parse_restricted_yaml(text: str, path: Path) -> dict[str, object]:
    """Parse a small data-only mapping subset with inline JSON values."""

    lines = text.splitlines()
    if len(lines) > MAX_MANIFEST_LINES:
        raise SchemaPackError(f"{path}: manifest has too many lines")
    root: dict[str, object] = {}
    stack: list[tuple[int, dict[str, object]]] = [(-2, root)]
    for number, raw_line in enumerate(lines, start=1):
        if len(raw_line) > MAX_LINE_CHARS:
            raise SchemaPackError(
                f"{_location(path, number)}: line is too long"
            )
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line:
            raise SchemaPackError(
                f"{_location(path, number)}: tabs are not supported"
            )
        indentation = len(raw_line) - len(raw_line.lstrip(" "))
        if indentation % 2:
            raise SchemaPackError(
                f"{_location(path, number)}: indentation must use two spaces"
            )
        content = raw_line[indentation:]
        if ":" not in content:
            raise SchemaPackError(
                f"{_location(path, number)}: expected key: value"
            )
        key, raw_value = content.split(":", 1)
        if FIELD_PATTERN.fullmatch(key) is None:
            raise SchemaPackError(
                f"{_location(path, number)}: invalid mapping key {key!r}"
            )
        while indentation <= stack[-1][0]:
            stack.pop()
        parent_indent, parent = stack[-1]
        if indentation != parent_indent + 2:
            raise SchemaPackError(
                f"{_location(path, number)}: indentation skips a mapping level"
            )
        if key in parent:
            raise SchemaPackError(
                f"{_location(path, number)}: duplicate key {key!r}"
            )
        value = raw_value.strip()
        if not value:
            child: dict[str, object] = {}
            parent[key] = child
            stack.append((indentation, child))
        else:
            parent[key] = _parse_scalar(value, path, number)
    return root


def _required_string(
    mapping: dict[str, object],
    key: str,
    context: str,
    *,
    maximum: int,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SchemaPackError(f"{context}: {key} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise SchemaPackError(
            f"{context}: {key} is longer than {maximum} characters"
        )
    return value


def _optional_string(
    mapping: dict[str, object],
    key: str,
    context: str,
    *,
    maximum: int,
) -> str | None:
    if key not in mapping:
        return None
    return _required_string(mapping, key, context, maximum=maximum)


def _required_integer(
    mapping: dict[str, object], key: str, context: str, *, minimum: int
) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SchemaPackError(
            f"{context}: {key} must be an integer of at least {minimum}"
        )
    return value


def _validate_component(value: str, context: str) -> str:
    if len(value) > 63 or COMPONENT_PATTERN.fullmatch(value) is None:
        raise SchemaPackError(
            f"{context}: identifier must match "
            "[a-z0-9]+(?:-[a-z0-9]+)* and contain at most 63 characters"
        )
    return value


def _reject_unknown(
    mapping: dict[str, object], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise SchemaPackError(
            f"{context}: unsupported keys: {', '.join(unknown)}"
        )


def _split_url(value: str, message: str) -> SplitResult:
    """Split a URL, turning a malformed one into a SchemaPackError.

    `urlsplit` raises ValueError for inputs such as an unterminated IPv6
    literal, and both callers are reached with values that come straight from
    a pack manifest or an agent-supplied attribute. Letting that ValueError
    through turned a rejectable value into a traceback.
    """

    try:
        return urlsplit(value)
    except ValueError as exc:
        raise SchemaPackError(message) from exc


def _validate_url(value: str, context: str) -> None:
    parsed = _split_url(value, f"{context}: default must be an HTTP(S) URL")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SchemaPackError(f"{context}: default must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise SchemaPackError(f"{context}: URL credentials are not supported")


def _validate_scalar_type(value: object, kind: str, context: str) -> None:
    if kind in {"string", "text", "enum", "date", "datetime", "url"}:
        if not isinstance(value, str):
            raise SchemaPackError(f"{context}: default must be a string")
        if kind == "date":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise SchemaPackError(
                    f"{context}: default must be an ISO 8601 date"
                ) from exc
        elif kind == "datetime":
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise SchemaPackError(
                    f"{context}: default must be an ISO 8601 datetime"
                ) from exc
            if parsed.tzinfo is None:
                raise SchemaPackError(
                    f"{context}: datetime default must include a timezone"
                )
        elif kind == "url":
            _validate_url(value, context)
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaPackError(f"{context}: default must be an integer")
    elif kind == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise SchemaPackError(f"{context}: default must be a number")
    elif kind == "boolean" and not isinstance(value, bool):
        raise SchemaPackError(f"{context}: default must be a boolean")


def _clone_literal(value: object) -> object:
    if isinstance(value, list):
        return [_clone_literal(item) for item in value]
    return value


def _validate_runtime_scalar(value: object, kind: str, context: str) -> None:
    if kind in {"string", "text", "enum", "date", "datetime", "url"}:
        if not isinstance(value, str):
            raise SchemaPackError(f"{context} must be a string")
        if kind == "date":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise SchemaPackError(
                    f"{context} must be an ISO 8601 date"
                ) from exc
        elif kind == "datetime":
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise SchemaPackError(
                    f"{context} must be an ISO 8601 datetime"
                ) from exc
            if parsed.tzinfo is None:
                raise SchemaPackError(f"{context} must include a timezone")
        elif kind == "url":
            url_parts = _split_url(value, f"{context} must be an HTTP(S) URL")
            if url_parts.scheme not in {"http", "https"} or not url_parts.hostname:
                raise SchemaPackError(f"{context} must be an HTTP(S) URL")
            if url_parts.username or url_parts.password:
                raise SchemaPackError(
                    f"{context} must not contain URL credentials"
                )
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaPackError(f"{context} must be an integer")
        return
    if kind == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise SchemaPackError(f"{context} must be a finite number")
        return
    if kind == "boolean":
        if not isinstance(value, bool):
            raise SchemaPackError(f"{context} must be a boolean")
        return
    raise SchemaPackError(f"{context} uses unsupported scalar type {kind}")


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    kind: str
    required: bool
    searchable: bool
    default: object | None
    has_default: bool
    minimum: int | float | None
    maximum: int | float | None
    min_length: int | None
    max_length: int | None
    values: tuple[str, ...]
    items: str | None
    max_items: int | None
    # `((sibling, expected), ...)`. `expected is None` means "must merely be
    # present"; any other value means "must be present and equal to this".
    requires: tuple[tuple[str, object], ...]
    # This field must equal the hostname of the named sibling URL field.
    host_of: str | None
    # A URL field that must already be stored in its canonical form.
    normalized: bool

    def validate_value(self, value: object, context: str) -> None:
        """Validate one stored or requested value against this field."""

        if self.kind == "array":
            if not isinstance(value, list):
                raise SchemaPackError(f"{context} must be an array")
            if self.max_items is not None and len(value) > self.max_items:
                raise SchemaPackError(
                    f"{context} must contain at most {self.max_items} items"
                )
            for index, item in enumerate(value):
                _validate_runtime_scalar(
                    item,
                    self.items or "string",
                    f"{context}[{index}]",
                )
            if len(value) != len(set(value)):
                raise SchemaPackError(f"{context} values must be unique")
            return

        _validate_runtime_scalar(value, self.kind, context)
        if self.kind == "enum" and value not in self.values:
            raise SchemaPackError(
                f"{context} must be one of: {', '.join(self.values)}"
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise SchemaPackError(
                    f"{context} must be at least {self.minimum}"
                )
            if self.maximum is not None and value > self.maximum:
                raise SchemaPackError(
                    f"{context} must be at most {self.maximum}"
                )
        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise SchemaPackError(
                    f"{context} must contain at least {self.min_length} characters"
                )
            if self.max_length is not None and len(value) > self.max_length:
                raise SchemaPackError(
                    f"{context} must contain at most {self.max_length} characters"
                )

    def json_schema(self) -> dict[str, object]:
        type_names = {
            "string": "string",
            "text": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "date": "string",
            "datetime": "string",
            "url": "string",
            "enum": "string",
            "array": "array",
        }
        schema: dict[str, object] = {"type": type_names[self.kind]}
        if self.kind == "date":
            schema["format"] = "date"
        elif self.kind == "datetime":
            schema["format"] = "date-time"
        elif self.kind == "url":
            schema["format"] = "uri"
            schema["pattern"] = "^https?://"
        elif self.kind == "enum":
            schema["enum"] = list(self.values)
        elif self.kind == "array":
            item_kind = self.items or "string"
            item_type = "number" if item_kind == "number" else item_kind
            schema["items"] = {"type": item_type}
            if item_kind == "date":
                schema["items"] = {"type": "string", "format": "date"}
            elif item_kind == "datetime":
                schema["items"] = {
                    "type": "string",
                    "format": "date-time",
                }
            elif item_kind == "url":
                schema["items"] = {"type": "string", "format": "uri"}
            if self.max_items is not None:
                schema["maxItems"] = self.max_items
            schema["uniqueItems"] = True
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.min_length is not None:
            schema["minLength"] = self.min_length
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.has_default:
            schema["default"] = self.default
        return schema

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "type": self.kind,
            "required": self.required,
            "searchable": self.searchable,
        }
        if self.has_default:
            value["default"] = self.default
        for key, item in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("min_length", self.min_length),
            ("max_length", self.max_length),
            ("max_items", self.max_items),
        ):
            if item is not None:
                value[key] = item
        if self.values:
            value["values"] = list(self.values)
        if self.items is not None:
            value["items"] = self.items
        if self.requires:
            value["requires"] = {
                name: expected for name, expected in self.requires
            }
        if self.host_of is not None:
            value["host_of"] = self.host_of
        if self.normalized:
            value["normalized"] = True
        return value


@dataclass(frozen=True)
class TypeDefinition:
    qualified_id: str
    pack_id: str
    local_id: str
    version: int
    title: str
    description: str
    fields: dict[str, FieldDefinition]
    body_sections: tuple[str, ...]
    template_path: Path | None
    template_text: str | None
    source: str
    builtin: bool

    def validate_attributes(
        self,
        value: object,
        *,
        apply_defaults: bool,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise SchemaPackError(
                f"attributes for {self.qualified_id} must be an object"
            )
        unknown = sorted(set(value) - set(self.fields))
        if unknown:
            raise SchemaPackError(
                f"attributes for {self.qualified_id} contain unknown fields: "
                f"{', '.join(str(name) for name in unknown)}"
            )
        normalized: dict[str, object] = {}
        for name, field in sorted(self.fields.items()):
            if name in value:
                field_value = value[name]
            elif apply_defaults and field.has_default:
                field_value = _clone_literal(field.default)
            elif field.required:
                raise SchemaPackError(
                    f"attributes for {self.qualified_id} are missing required "
                    f"field {name}"
                )
            else:
                continue
            field.validate_value(
                field_value,
                f"attribute {self.qualified_id}.{name}",
            )
            normalized[name] = field_value
        return normalized

    def initial_body(self) -> str:
        if self.template_text is not None:
            return self.template_text.strip()
        return "\n\n".join(f"## {section}" for section in self.body_sections)

    def attributes_json_schema(self) -> dict[str, object]:
        required = [
            field.name for field in self.fields.values() if field.required
        ]
        schema: dict[str, object] = {
            "type": "object",
            "properties": {
                name: field.json_schema()
                for name, field in sorted(self.fields.items())
            },
            "additionalProperties": False,
        }
        if required:
            schema["required"] = sorted(required)
        return schema

    def attributes_patch_json_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                name: {
                    "anyOf": [
                        field.json_schema(),
                        {"type": "null"},
                    ]
                }
                for name, field in sorted(self.fields.items())
            },
            "additionalProperties": False,
            "minProperties": 1,
        }

    def search_filters_json_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                name: field.json_schema()
                for name, field in sorted(self.fields.items())
                if field.searchable
            },
            "additionalProperties": False,
            "minProperties": 1,
        }

    def entry_json_schema(self) -> dict[str, object]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"urn:knowledge:type:{self.qualified_id}:{self.version}",
            "title": self.title,
            "type": "object",
            "required": ["type", "type_version", "attributes"],
            "properties": {
                "type": {"const": self.qualified_id},
                "type_version": {"const": self.version},
                "attributes": self.attributes_json_schema(),
            },
        }

    def as_dict(self, *, include_schema: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.qualified_id,
            "pack_id": self.pack_id,
            "local_id": self.local_id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "fields": [
                field.as_dict()
                for _, field in sorted(self.fields.items())
            ],
            "body_sections": list(self.body_sections),
            "has_template": self.template_path is not None,
            "source": self.source,
            "builtin": self.builtin,
        }
        if include_schema:
            value["json_schema"] = self.entry_json_schema()
        return value


@dataclass(frozen=True)
class PackDefinition:
    pack_id: str
    version: int
    title: str
    description: str
    types: dict[str, TypeDefinition]
    manifest_path: Path
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.pack_id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "type_ids": sorted(self.types),
            "source": self.source,
        }


def _field_definition(
    name: str, raw: object, context: str
) -> FieldDefinition:
    if FIELD_PATTERN.fullmatch(name) is None:
        raise SchemaPackError(f"{context}: invalid field identifier {name!r}")
    if not isinstance(raw, dict):
        raise SchemaPackError(f"{context}: field definition must be a mapping")
    _reject_unknown(raw, FIELD_KEYS, context)
    kind = _required_string(raw, "type", context, maximum=40)
    if kind not in SUPPORTED_FIELD_TYPES:
        raise SchemaPackError(f"{context}: unsupported field type {kind!r}")
    required = raw.get("required", False)
    searchable = raw.get("searchable", False)
    if not isinstance(required, bool) or not isinstance(searchable, bool):
        raise SchemaPackError(
            f"{context}: required and searchable must be booleans"
        )
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    for key, value in (("minimum", minimum), ("maximum", maximum)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise SchemaPackError(f"{context}: {key} must be a number")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise SchemaPackError(f"{context}: minimum exceeds maximum")
    if (minimum is not None or maximum is not None) and kind not in {
        "integer",
        "number",
    }:
        raise SchemaPackError(
            f"{context}: numeric bounds require integer or number type"
        )
    min_length = raw.get("min_length")
    max_length = raw.get("max_length")
    for key, value in (("min_length", min_length), ("max_length", max_length)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise SchemaPackError(
                f"{context}: {key} must be a non-negative integer"
            )
    if (
        min_length is not None
        and max_length is not None
        and min_length > max_length
    ):
        raise SchemaPackError(f"{context}: min_length exceeds max_length")
    if (min_length is not None or max_length is not None) and kind not in {
        "string",
        "text",
    }:
        raise SchemaPackError(
            f"{context}: length bounds require string or text type"
        )

    raw_values = raw.get("values", [])
    if not isinstance(raw_values, list) or not all(
        isinstance(item, str) and item for item in raw_values
    ):
        raise SchemaPackError(f"{context}: values must be non-empty strings")
    if len(raw_values) != len(set(raw_values)):
        raise SchemaPackError(f"{context}: enum values must be unique")
    if kind == "enum" and not raw_values:
        raise SchemaPackError(f"{context}: enum requires values")
    if kind != "enum" and raw_values:
        raise SchemaPackError(f"{context}: values are only valid for enum")

    items = raw.get("items")
    max_items = raw.get("max_items")
    if kind == "array":
        if not isinstance(items, str) or items not in ARRAY_ITEM_TYPES:
            raise SchemaPackError(
                f"{context}: array items must name a supported scalar type"
            )
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items < 1
            or max_items > 1_000
        ):
            raise SchemaPackError(
                f"{context}: array max_items must be an integer from 1 to 1000"
            )
    elif items is not None or max_items is not None:
        raise SchemaPackError(
            f"{context}: items and max_items are only valid for arrays"
        )

    has_default = "default" in raw
    default = raw.get("default")
    if has_default:
        if kind == "array":
            if not isinstance(default, list):
                raise SchemaPackError(f"{context}: array default must be a list")
            if max_items is not None and len(default) > max_items:
                raise SchemaPackError(
                    f"{context}: array default exceeds max_items"
                )
            for index, value in enumerate(default):
                _validate_scalar_type(value, str(items), f"{context}[{index}]")
            if len(default) != len(set(default)):
                raise SchemaPackError(
                    f"{context}: array default values must be unique"
                )
        else:
            _validate_scalar_type(default, kind, context)
        if kind == "enum" and default not in raw_values:
            raise SchemaPackError(
                f"{context}: enum default is not one of its values"
            )
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            if minimum is not None and default < minimum:
                raise SchemaPackError(f"{context}: default is below minimum")
            if maximum is not None and default > maximum:
                raise SchemaPackError(f"{context}: default exceeds maximum")
        if isinstance(default, str):
            if min_length is not None and len(default) < min_length:
                raise SchemaPackError(f"{context}: default is shorter than min_length")
            if max_length is not None and len(default) > max_length:
                raise SchemaPackError(f"{context}: default is longer than max_length")

    raw_requires = raw.get("requires", {})
    if not isinstance(raw_requires, dict):
        raise SchemaPackError(f"{context}: requires must be a mapping")
    if len(raw_requires) > MAX_FIELDS_PER_TYPE:
        raise SchemaPackError(f"{context}: requires names too many fields")
    requires: list[tuple[str, object]] = []
    for sibling, expected in sorted(raw_requires.items()):
        if not isinstance(sibling, str) or FIELD_PATTERN.fullmatch(sibling) is None:
            raise SchemaPackError(
                f"{context}: requires must name fields, not {sibling!r}"
            )
        if sibling == name:
            raise SchemaPackError(f"{context}: requires must name another field")
        if expected is not None and not isinstance(expected, REQUIRES_VALUE_TYPES):
            raise SchemaPackError(
                f"{context}: requires values must be a literal or null"
            )
        requires.append((sibling, expected))

    host_of = raw.get("host_of")
    if host_of is not None:
        if not isinstance(host_of, str) or FIELD_PATTERN.fullmatch(host_of) is None:
            raise SchemaPackError(f"{context}: host_of must name a field")
        if host_of == name:
            raise SchemaPackError(f"{context}: host_of must name another field")
        if kind != "string":
            raise SchemaPackError(f"{context}: host_of requires string type")

    normalized = raw.get("normalized", False)
    if not isinstance(normalized, bool):
        raise SchemaPackError(f"{context}: normalized must be a boolean")
    if normalized and kind != "url":
        raise SchemaPackError(f"{context}: normalized requires url type")

    return FieldDefinition(
        name=name,
        kind=kind,
        required=required,
        searchable=searchable,
        default=default,
        has_default=has_default,
        minimum=minimum,
        maximum=maximum,
        min_length=min_length,
        max_length=max_length,
        values=tuple(raw_values),
        items=items if isinstance(items, str) else None,
        max_items=max_items if isinstance(max_items, int) else None,
        requires=tuple(requires),
        host_of=host_of,
        normalized=normalized,
    )


def _template_path(
    pack_root: Path,
    value: object,
    context: str,
) -> tuple[Path | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise SchemaPackError(f"{context}: template must be a short relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
        raise SchemaPackError(
            f"{context}: template must be a relative .md path below the pack"
        )
    candidate = pack_root / relative
    if candidate.is_symlink():
        raise SchemaPackError(f"{context}: template must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SchemaPackError(f"{context}: template does not exist") from exc
    if not resolved.is_relative_to(pack_root) or not resolved.is_file():
        raise SchemaPackError(f"{context}: template resolves outside the pack")
    if resolved.stat().st_size > MAX_TEMPLATE_BYTES:
        raise SchemaPackError(f"{context}: template is too large")
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaPackError(f"{context}: template must be UTF-8 text") from exc
    return resolved, text


def _load_pack(pack_root: Path, source: str) -> PackDefinition:
    manifest = pack_root / "pack.yaml"
    if manifest.is_symlink():
        raise SchemaPackError(f"{manifest}: manifest must not be a symbolic link")
    if not manifest.is_file():
        raise SchemaPackError(f"{pack_root}: pack.yaml is required")
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise SchemaPackError(f"{manifest}: manifest is too large")
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaPackError(f"{manifest}: manifest must be UTF-8 text") from exc
    raw = parse_restricted_yaml(text, manifest)
    context = str(manifest)
    _reject_unknown(raw, PACK_KEYS, context)
    format_version = _required_integer(
        raw, "format_version", context, minimum=1
    )
    if format_version != PACK_FORMAT_VERSION:
        raise SchemaPackError(
            f"{context}: unsupported format_version {format_version}"
        )
    pack_id = _validate_component(
        _required_string(raw, "id", context, maximum=63),
        context,
    )
    if pack_root.name != pack_id:
        raise SchemaPackError(
            f"{context}: pack directory must be named {pack_id!r}"
        )
    version = _required_integer(raw, "version", context, minimum=1)
    title = _required_string(raw, "title", context, maximum=200)
    description = _required_string(
        raw,
        "description",
        context,
        maximum=MAX_DESCRIPTION_CHARS,
    )
    raw_types = raw.get("types")
    if not isinstance(raw_types, dict) or not raw_types:
        raise SchemaPackError(f"{context}: types must be a non-empty mapping")
    if len(raw_types) > MAX_TYPES_PER_PACK:
        raise SchemaPackError(f"{context}: pack defines too many types")
    types: dict[str, TypeDefinition] = {}
    for local_id, raw_type in sorted(raw_types.items()):
        type_context = f"{context}: type {local_id}"
        if not isinstance(local_id, str):
            raise SchemaPackError(f"{context}: type IDs must be strings")
        _validate_component(local_id, type_context)
        if not isinstance(raw_type, dict):
            raise SchemaPackError(f"{type_context}: definition must be a mapping")
        _reject_unknown(raw_type, TYPE_KEYS, type_context)
        entry_type = raw_type.get("entry_type")
        if entry_type is None:
            qualified_id = f"{pack_id}/{local_id}"
            if len(qualified_id) > 127:
                raise SchemaPackError(
                    f"{type_context}: qualified ID is too long"
                )
            builtin = False
        else:
            if source == "local":
                raise SchemaPackError(
                    f"{type_context}: local packs cannot define short built-in "
                    "entry types"
                )
            if not isinstance(entry_type, str):
                raise SchemaPackError(
                    f"{type_context}: entry_type must be a string"
                )
            qualified_id = _validate_component(entry_type, type_context)
            if qualified_id != local_id:
                raise SchemaPackError(
                    f"{type_context}: entry_type must match its local type ID"
                )
            builtin = True
        type_title = _required_string(
            raw_type, "title", type_context, maximum=200
        )
        type_description = _required_string(
            raw_type,
            "description",
            type_context,
            maximum=MAX_DESCRIPTION_CHARS,
        )
        raw_fields = raw_type.get("fields", {})
        if not isinstance(raw_fields, dict):
            raise SchemaPackError(f"{type_context}: fields must be a mapping")
        if len(raw_fields) > MAX_FIELDS_PER_TYPE:
            raise SchemaPackError(f"{type_context}: too many fields")
        fields = {
            name: _field_definition(
                name,
                raw_field,
                f"{type_context}: field {name}",
            )
            for name, raw_field in sorted(raw_fields.items())
        }
        for field in fields.values():
            for sibling, _ in field.requires:
                if sibling not in fields:
                    raise SchemaPackError(
                        f"{type_context}: field {field.name} requires unknown "
                        f"field {sibling}"
                    )
            if field.host_of is not None:
                target = fields.get(field.host_of)
                if target is None or target.kind != "url":
                    raise SchemaPackError(
                        f"{type_context}: field {field.name} host_of must name "
                        "a url field"
                    )
        raw_sections = raw_type.get("body_sections", [])
        if not isinstance(raw_sections, list) or not all(
            isinstance(section, str)
            and section.strip()
            and len(section.strip()) <= 200
            for section in raw_sections
        ):
            raise SchemaPackError(
                f"{type_context}: body_sections must be short non-empty strings"
            )
        sections = tuple(section.strip() for section in raw_sections)
        if len(sections) != len(set(sections)) or len(sections) > 32:
            raise SchemaPackError(
                f"{type_context}: body_sections must be unique and limited to 32"
            )
        template, template_text = _template_path(
            pack_root,
            raw_type.get("template"),
            type_context,
        )
        types[qualified_id] = TypeDefinition(
            qualified_id=qualified_id,
            pack_id=pack_id,
            local_id=local_id,
            version=version,
            title=type_title,
            description=type_description,
            fields=fields,
            body_sections=sections,
            template_path=template,
            template_text=template_text,
            source=source,
            builtin=builtin,
        )
    return PackDefinition(
        pack_id=pack_id,
        version=version,
        title=title,
        description=description,
        types=types,
        manifest_path=manifest,
        source=source,
    )


def load_pack_candidate(value: str | Path) -> PackDefinition:
    """Validate one uninstalled local pack directory without installing it."""

    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise SchemaPackError(f"{raw}: pack directory must not be a symbolic link")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise SchemaPackError(f"{raw}: pack directory is unavailable") from exc
    if path.is_file() and path.name == "pack.yaml":
        path = path.parent
    if not path.is_dir():
        raise SchemaPackError(f"{path}: pack candidate must be a directory")
    return _load_pack(path, "local")


@dataclass(frozen=True)
class SchemaRegistry:
    packs: dict[str, PackDefinition]
    types: dict[str, TypeDefinition]

    # Both were plain properties that rebuilt a dict on every access. They are
    # read inside the search loop and twice per entry during validation, which
    # at 10 000 entries and 13 installed types meant ~130 000 dict inserts per
    # `search` for a mapping that cannot change while the process runs.
    # `cached_property` writes straight into `__dict__`, so it works on a
    # frozen dataclass -- `__setattr__`, which the freeze blocks, is not used.
    @functools.cached_property
    def custom_types(self) -> dict[str, TypeDefinition]:
        return {
            type_id: definition
            for type_id, definition in self.types.items()
            if not definition.builtin
        }

    @functools.cached_property
    def builtin_types(self) -> dict[str, TypeDefinition]:
        return {
            type_id: definition
            for type_id, definition in self.types.items()
            if definition.builtin
        }

    @classmethod
    def load(cls, layout: NoetrailLayout) -> SchemaRegistry:
        packs: dict[str, PackDefinition] = {}
        types: dict[str, TypeDefinition] = {}
        builtins_root = layout.builtins_packs_root
        local_root = layout.local_packs_root
        roots = (
            (("combined", builtins_root),)
            if builtins_root == local_root
            else (
                ("built-in", builtins_root),
                ("local", local_root),
            )
        )
        for source, root in roots:
            if not root.exists():
                continue
            for candidate in sorted(root.iterdir(), key=lambda path: path.name):
                if candidate.name.startswith("."):
                    raise SchemaPackError(
                        f"{candidate}: hidden pack entries are not supported"
                    )
                if candidate.is_symlink():
                    raise SchemaPackError(
                        f"{candidate}: pack directory must not be a symbolic link"
                    )
                if not candidate.is_dir():
                    raise SchemaPackError(
                        f"{candidate}: pack root may contain only directories"
                    )
                pack = _load_pack(candidate.resolve(strict=True), source)
                if pack.pack_id in packs:
                    existing = packs[pack.pack_id]
                    raise SchemaPackError(
                        f"duplicate pack ID {pack.pack_id!r} in "
                        f"{existing.manifest_path} and {pack.manifest_path}"
                    )
                packs[pack.pack_id] = pack
                for qualified_id, type_definition in pack.types.items():
                    if qualified_id in types:
                        raise SchemaPackError(
                            f"duplicate type ID {qualified_id!r}"
                        )
                    types[qualified_id] = type_definition
                if len(packs) > MAX_PACKS:
                    raise SchemaPackError("installation contains too many packs")
        return cls(packs=packs, types=types)

    def require_type(self, qualified_id: str) -> TypeDefinition:
        try:
            return self.types[qualified_id]
        except KeyError as exc:
            raise SchemaPackError(
                f"schema type does not exist: {qualified_id}"
            ) from exc

    def summary(self) -> dict[str, object]:
        return {
            "valid": True,
            "pack_count": len(self.packs),
            "type_count": len(self.types),
            "packs": [
                pack.as_dict() for _, pack in sorted(self.packs.items())
            ],
            "types": [
                type_definition.as_dict()
                for _, type_definition in sorted(self.types.items())
            ],
        }

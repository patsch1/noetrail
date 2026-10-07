#!/usr/bin/env python3
"""Reading and writing entry frontmatter, and its type envelope.

Noetrail writes exactly one dialect: YAML frontmatter whose values are JSON on
a single line. It is compact, it diffs well, and it needs no YAML dependency to
produce.

Reading is deliberately wider, and normalizes back to the canonical form on the
next write. Tolerance stops where a value could silently change meaning:
anchors, aliases, tags, merge keys, complex keys, multi-line plain scalars, and
duplicate keys are refused with a line number rather than guessed at, because
misreading a personal entry is worse than refusing to read it.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import NoReturn

from noetrail.constants import (
    ATTRIBUTE_ENVELOPE_VERSIONS,
    BUILTIN_CORE_FIELDS,
    COMPATIBLE_SCHEMA_VERSIONS,
    ENTRY_TYPES,
    MAX_FRONTMATTER_DEPTH,
    MAX_FRONTMATTER_KEY_CHARS,
    MAX_FRONTMATTER_LINE_CHARS,
    MAX_FRONTMATTER_LINES,
)
from noetrail.errors import (
    ConfigurationError,
    InvalidRequest,
    UnsupportedSchema,
    ValidationError,
)
from noetrail.migrations import (
    get_schema_version,
)
from noetrail.schema import SchemaPackError, SchemaRegistry, TypeDefinition

NUMBER_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$")
NULL_VALUES = {"", "~", "null", "Null", "NULL"}
TRUE_VALUES = {"true", "True", "TRUE"}
FALSE_VALUES = {"false", "False", "FALSE"}
BLOCK_SCALAR_PATTERN = re.compile(r"^([|>])([-+]?)$")
# Constructs whose meaning depends on state outside the value itself. The same
# set `obsidian.parse_properties` refuses, for the same reason.
UNSUPPORTED_PREFIXES = ("&", "*", "!", "<<", "?", "%", "@", "`")


class FrontmatterError(ValueError):
    """Frontmatter is missing, malformed, or uses an unsupported construct.

    Subclasses `ValueError` because every caller already treats an unreadable
    entry as one: a vault scan skips it and counts it, `validate` reports it
    with its path.
    """


class BuiltinEntryMetadata(dict[str, object]):
    """Expose schema-9 attributes through the legacy mapping interface.

    The serialized mapping remains untouched: ``items()`` still yields the
    core envelope plus ``attributes``. Trusted workflow code can meanwhile
    read and mutate domain keys as before during the compatibility window.
    """

    def _attributes(self) -> dict[str, object]:
        value = dict.get(self, "attributes")
        return value if isinstance(value, dict) else {}

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or key in self._attributes()

    def __getitem__(self, key: str) -> object:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return self._attributes()[key]

    def get(self, key: str, default: object = None) -> object:
        if dict.__contains__(self, key):
            return dict.get(self, key, default)
        return self._attributes().get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        if key not in BUILTIN_CORE_FIELDS and key not in {"type_version", "attributes"}:
            self._attributes()[key] = value
            return
        dict.__setitem__(self, key, value)

    def pop(self, key: str, default: object = None) -> object:
        if dict.__contains__(self, key):
            return dict.pop(self, key)
        return self._attributes().pop(key, default)


def entry_type_name(metadata: dict[str, object]) -> str | None:
    """Return the entry's `type` if it is a string, otherwise None.

    Frontmatter values are arbitrary JSON, so `type` can hold a list or an
    object. Both are unhashable, and every membership test against a set of
    known types and every registry lookup then raises TypeError instead of
    reporting an invalid entry. Callers that only need the name go through
    here; callers that must report the raw value keep using `metadata.get`.
    """

    entry_type = metadata.get("type")
    return entry_type if isinstance(entry_type, str) else None


def metadata_list(metadata: dict[str, object], key: str) -> list[object]:
    """Return a frontmatter list field, or an empty list if it is not one.

    Same reason as `entry_type_name`: a read path that iterates `tags` or
    `relations` must not abort for the whole vault because one entry stores a
    number there. `validate` is what reports the malformed field.
    """

    value = metadata.get(key)
    return value if isinstance(value, list) else []


def uses_attribute_envelope(metadata: dict[str, object]) -> bool:
    """Report whether the entry stores domain fields under `attributes`."""

    return metadata.get("schema_version") in ATTRIBUTE_ENVELOPE_VERSIONS


def runtime_metadata(metadata: dict[str, object]) -> dict[str, object]:
    if (
        uses_attribute_envelope(metadata)
        and entry_type_name(metadata) in ENTRY_TYPES
        and isinstance(metadata.get("attributes"), dict)
    ):
        return BuiltinEntryMetadata(metadata)
    return metadata


def builtin_type_layouts(
    registry: SchemaRegistry,
) -> dict[str, tuple[int, tuple[str, ...], dict[str, object]]]:
    return {
        type_id: (
            definition.version,
            tuple(definition.fields),
            {
                name: field.default
                for name, field in definition.fields.items()
                if field.has_default
            },
        )
        for type_id, definition in registry.builtin_types.items()
    }


def pack_builtin_metadata(
    metadata: dict[str, object],
    registry: SchemaRegistry,
) -> dict[str, object]:
    entry_type = metadata.get("type")
    if not isinstance(entry_type, str):
        raise ValidationError("Built-in entry has no valid type")
    definition = registry.builtin_types.get(entry_type)
    if definition is None:
        raise ConfigurationError(
            f"Built-in type definition is unavailable for {entry_type!r}"
        )
    attributes = {
        field_name: metadata[field_name]
        for field_name in definition.fields
        if field_name in metadata
    }
    try:
        attributes = definition.validate_attributes(
            attributes,
            apply_defaults=True,
        )
    except SchemaPackError as exc:
        raise InvalidRequest(str(exc)) from exc

    packed: dict[str, object] = {}
    inserted = False
    for key, value in metadata.items():
        if key in definition.fields:
            continue
        packed[key] = value
        if key == "type":
            packed["type_version"] = definition.version
            packed["attributes"] = attributes
            inserted = True
    if not inserted:
        packed["type_version"] = definition.version
        packed["attributes"] = attributes
    return packed


def parse_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    """Read one entry file and return its metadata and body."""

    return parse_frontmatter_text(path.read_text(encoding="utf-8"))


def parse_frontmatter_text(text: str) -> tuple[dict[str, object], str]:
    """Parse frontmatter and body from entry text.

    Noetrail writes one dialect and reads a wider one. An entry is a plain
    Markdown file, so an ordinary tool may rewrite its frontmatter in the block
    style YAML normally uses -- and with `import obsidian` shipped, a user is
    now actively invited into a vault where that happens. If only the written
    dialect were readable, such an edit would make a personal entry unreadable
    to the program that owns it.

    Errors name a line number and never the path, because callers report the
    path themselves. `FrontmatterError` subclasses `ValueError`, which is what
    every caller already treats as an unreadable entry.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("missing opening frontmatter delimiter")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], 1)
            if line.strip() in {"---", "..."}
        )
    except StopIteration as exc:
        raise FrontmatterError("missing closing frontmatter delimiter") from exc

    region = lines[1:end]
    if len(region) > MAX_FRONTMATTER_LINES:
        raise FrontmatterError("frontmatter has too many lines")
    for number, line in enumerate(region, 2):
        if len(line) > MAX_FRONTMATTER_LINE_CHARS:
            raise FrontmatterError(f"line {number} is too long")

    metadata = _parse_canonical(region)
    if metadata is None:
        metadata = _parse_block(region)
    # H3: the body used to be `.strip()`ed here and `.rstrip()`ed on write, so
    # an entry whose body opened with an indented code block lost that
    # indentation on the next write of any kind. Only the single blank line
    # that `render_entry` inserts after the closing delimiter is consumed.
    body_lines = lines[end + 1 :]
    if body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    body = "\n".join(body_lines).rstrip("\n")
    return metadata, body


def _parse_canonical(region: list[str]) -> dict[str, object] | None:
    """Parse the dialect Noetrail writes, or report that it does not apply.

    This is the hot path: an unindexed search parses every entry in the vault,
    so the common case stays a split and a JSON decode. Anything else falls
    back to the tolerant parser, which may be slower because it runs only on a
    file another tool rewrote, and only until that file is next written.
    """

    metadata: dict[str, object] = {}
    for number, raw_line in enumerate(region, 2):
        if not raw_line.strip():
            continue
        if raw_line[:1].isspace() or ":" not in raw_line:
            return None
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            return None
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            return None
        # The pack parser has rejected duplicate keys from the start; this one
        # took the last value silently, so a bad merge or sync could overwrite
        # `id`, `title` or `status` without `validate` ever noticing.
        if key in metadata:
            raise FrontmatterError(
                f"duplicate frontmatter key {key!r} on line {number}"
            )
        metadata[key] = value
    return metadata


class _Reader:
    """A cursor over frontmatter lines that can rewrite the current line.

    Rewriting is how a sequence item such as ``- predicate: mentions`` is
    parsed: the dash is replaced by spaces and the remainder is handed to the
    ordinary mapping parser at the resulting column. That keeps one
    implementation for block mappings instead of a second one for mappings
    nested in sequences.
    """

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.index = 0

    def peek(self) -> tuple[int, int, str] | None:
        index = self.index
        while index < len(self.lines):
            line = self.lines[index]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            self.index = index
            if "\t" in line[: len(line) - len(line.lstrip())]:
                self.fail("tabs are not supported for indentation", index)
            indentation = len(line) - len(line.lstrip(" "))
            return index, indentation, _strip_comment(line[indentation:])
        self.index = index
        return None

    def advance(self) -> None:
        self.index += 1

    def replace(self, index: int, line: str) -> None:
        self.lines[index] = line
        self.index = index

    def fail(self, message: str, index: int) -> NoReturn:
        raise FrontmatterError(f"{message} on line {index + 2}")


def _parse_block(region: list[str]) -> dict[str, object]:
    reader = _Reader(region)
    metadata = _parse_mapping(reader, 0, 0)
    remaining = reader.peek()
    if remaining is not None:
        reader.fail("unsupported frontmatter syntax", remaining[0])
    return metadata


def _parse_mapping(reader: _Reader, indent: int, depth: int) -> dict[str, object]:
    _check_depth(reader, depth)
    result: dict[str, object] = {}
    while True:
        item = reader.peek()
        if item is None:
            break
        index, indentation, content = item
        if indentation < indent:
            break
        if indentation > indent:
            reader.fail(
                "unexpected indentation; multi-line plain values must be "
                "quoted or written as a block scalar",
                index,
            )
        if content == "-" or content.startswith("- "):
            break
        separator = _find_key_separator(content)
        if separator is None:
            reader.fail("expected 'key: value'", index)
        key = _parse_key(reader, content[:separator], index)
        if key in result:
            raise FrontmatterError(
                f"duplicate frontmatter key {key!r} on line {index + 2}"
            )
        rest = content[separator + 1 :].strip()
        reader.advance()
        if rest:
            result[key] = _parse_inline_value(reader, rest, index, indent, depth)
        else:
            result[key] = _parse_block_value(reader, indent, depth)
    return result


def _parse_sequence(reader: _Reader, indent: int, depth: int) -> list[object]:
    _check_depth(reader, depth)
    items: list[object] = []
    while True:
        item = reader.peek()
        if item is None:
            break
        index, indentation, content = item
        if indentation != indent:
            break
        if content == "-":
            reader.advance()
            items.append(_parse_block_value(reader, indent, depth))
            continue
        if not content.startswith("- "):
            break
        remainder = content[1:]
        offset = len(remainder) - len(remainder.lstrip(" "))
        value_indent = indent + 1 + offset
        value_text = remainder.lstrip(" ")
        if _find_key_separator(value_text) is not None:
            reader.replace(index, " " * value_indent + value_text)
            items.append(_parse_mapping(reader, value_indent, depth + 1))
            continue
        if value_text == "-" or value_text.startswith("- "):
            reader.replace(index, " " * value_indent + value_text)
            items.append(_parse_sequence(reader, value_indent, depth + 1))
            continue
        reader.advance()
        items.append(
            _parse_inline_value(reader, value_text, index, value_indent, depth)
        )
    return items


def _parse_block_value(reader: _Reader, indent: int, depth: int) -> object:
    """Parse the value of a key whose own line ended after the colon."""

    item = reader.peek()
    if item is None:
        return None
    _index, indentation, content = item
    is_sequence_item = content == "-" or content.startswith("- ")
    if indentation > indent:
        if is_sequence_item:
            return _parse_sequence(reader, indentation, depth + 1)
        return _parse_mapping(reader, indentation, depth + 1)
    if indentation == indent and is_sequence_item:
        # YAML allows a block sequence at its parent key's own column.
        return _parse_sequence(reader, indentation, depth + 1)
    return None


def _parse_inline_value(
    reader: _Reader,
    text: str,
    index: int,
    indent: int,
    depth: int,
) -> object:
    del depth
    header = BLOCK_SCALAR_PATTERN.fullmatch(text)
    if header is not None:
        return _parse_block_scalar(reader, indent, header.group(1), header.group(2))
    return _parse_scalar(reader, text, index)


def _parse_block_scalar(
    reader: _Reader,
    indent: int,
    style: str,
    chomping: str,
) -> str:
    collected: list[str] = []
    content_indent: int | None = None
    while reader.index < len(reader.lines):
        line = reader.lines[reader.index]
        if line.strip():
            indentation = len(line) - len(line.lstrip(" "))
            if indentation <= indent:
                break
            if content_indent is None:
                content_indent = indentation
            collected.append(line[content_indent:] if content_indent else line)
        else:
            collected.append("")
        reader.advance()

    while collected and not collected[-1]:
        collected.pop()
    if not collected:
        return ""
    if style == "|":
        value = "\n".join(collected)
    else:
        value = _fold(collected)
    if chomping == "-":
        return value
    return value + "\n"


def _fold(collected: list[str]) -> str:
    parts: list[str] = []
    for line in collected:
        if not line:
            parts.append("\n")
        elif parts and parts[-1] not in {"\n", ""} and not parts[-1].endswith("\n"):
            parts.append(" " + line)
        else:
            parts.append(line)
    return "".join(parts)


def _parse_scalar(reader: _Reader, text: str, index: int) -> object:
    if text.startswith(('"', "[", "{")):
        return _decode_json(reader, text, index)
    if text.startswith("'"):
        return _decode_single_quoted(reader, text, index)
    if text.startswith(UNSUPPORTED_PREFIXES):
        reader.fail(
            "YAML anchors, aliases, tags, merge keys, complex keys, and "
            "directives are not supported",
            index,
        )
    if any(ord(character) < 32 for character in text):
        reader.fail("control characters are not supported", index)
    plain = _strip_trailing_comment(text)
    if plain in NULL_VALUES:
        return None
    if plain in TRUE_VALUES:
        return True
    if plain in FALSE_VALUES:
        return False
    if NUMBER_PATTERN.fullmatch(plain):
        return json.loads(plain)
    return plain


def _decode_json(reader: _Reader, text: str, index: int) -> object:
    try:
        value, consumed = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise FrontmatterError(
            f"value must use JSON-compatible syntax on line {index + 2}"
        ) from exc
    _require_only_comment(reader, text[consumed:], index)
    return value


def _decode_single_quoted(reader: _Reader, text: str, index: int) -> str:
    position = 1
    parts: list[str] = []
    while position < len(text):
        character = text[position]
        if character != "'":
            parts.append(character)
            position += 1
            continue
        if text[position + 1 : position + 2] == "'":
            parts.append("'")
            position += 2
            continue
        _require_only_comment(reader, text[position + 1 :], index)
        return "".join(parts)
    return reader.fail("unterminated quoted value", index)


def _require_only_comment(reader: _Reader, remainder: str, index: int) -> None:
    trailing = remainder.strip()
    if trailing and not trailing.startswith("#"):
        reader.fail("unexpected text after a value", index)


def _find_key_separator(content: str) -> int | None:
    """Return the index of the colon that separates a key from its value.

    A colon only separates when a space or the end of the line follows it, so
    plain values such as ``https://example.com`` and an ISO timestamp are not
    mistaken for keys.
    """

    if content[:1] in {'"', "'"}:
        quote = content[0]
        position = 1
        while position < len(content):
            character = content[position]
            if quote == '"' and character == "\\":
                position += 2
                continue
            if character == quote:
                if quote == "'" and content[position + 1 : position + 2] == "'":
                    position += 2
                    continue
                break
            position += 1
        else:
            return None
        return position + 1 if content[position + 1 : position + 2] == ":" else None
    for position, character in enumerate(content):
        if character == "#" and position and content[position - 1] == " ":
            return None
        if character == ":" and content[position + 1 : position + 2] in {"", " "}:
            return position
    return None


def _parse_key(reader: _Reader, text: str, index: int) -> str:
    key = text.strip()
    if key[:1] in {'"', "'"}:
        decoded = (
            _decode_json(reader, key, index)
            if key[0] == '"'
            else _decode_single_quoted(reader, key, index)
        )
        if not isinstance(decoded, str):
            reader.fail("a mapping key must be a string", index)
        key = str(decoded)
    else:
        if key.startswith(UNSUPPORTED_PREFIXES):
            reader.fail(
                "YAML anchors, aliases, tags, merge keys, complex keys, and "
                "directives are not supported",
                index,
            )
    if not key:
        reader.fail("a mapping key must not be empty", index)
    if len(key) > MAX_FRONTMATTER_KEY_CHARS:
        reader.fail("mapping key is too long", index)
    if any(ord(character) < 32 for character in key):
        reader.fail("control characters are not supported", index)
    return key


def _strip_comment(content: str) -> str:
    if content.startswith("#"):
        return ""
    return content.rstrip()


def _strip_trailing_comment(text: str) -> str:
    """Cut a YAML comment from a plain scalar.

    A ``#`` only starts a comment when a space precedes it, which is why a
    value such as ``rum#3`` survives while ``rum # 3`` does not. Quoted values
    never reach this function.
    """

    position = text.find(" #")
    return text if position < 0 else text[:position].rstrip()


def _check_depth(reader: _Reader, depth: int) -> None:
    if depth > MAX_FRONTMATTER_DEPTH:
        item = reader.peek()
        reader.fail("frontmatter is nested too deeply", item[0] if item else 0)


def render_entry(metadata: dict[str, object], body: str) -> str:
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in metadata.items()
    )
    # Only the trailing newline is normalised. Stripping leading whitespace
    # here would silently reformat bodies that legitimately start with an
    # indented code block.
    lines.extend(["---", "", body.rstrip("\n"), ""])
    return "\n".join(lines)


def is_current_schema(metadata: dict[str, object]) -> bool:
    """Report whether an entry can be read and written by this version.

    Read commands that walk the whole vault use this to skip an incompatible
    entry and say so in their result. Commands that write a *specific* entry
    use `require_current_schema` instead and refuse outright.
    """
    try:
        return get_schema_version(metadata) in COMPATIBLE_SCHEMA_VERSIONS
    except ValueError:
        return False


def require_current_schema(metadata: dict[str, object]) -> None:
    try:
        version = get_schema_version(metadata)
    except ValueError as exc:
        raise UnsupportedSchema(str(exc)) from exc
    if version not in COMPATIBLE_SCHEMA_VERSIONS:
        raise UnsupportedSchema(
            f"Entry uses schema version {version}; run "
            f"`noetrail migrate` before editing"
        )


def require_supported_entry_type(
    metadata: dict[str, object],
    registry: SchemaRegistry,
) -> TypeDefinition | None:
    entry_type = entry_type_name(metadata)
    if entry_type in ENTRY_TYPES:
        assert entry_type is not None
        if uses_attribute_envelope(metadata):
            definition = registry.builtin_types.get(entry_type)
            if definition is None:
                raise ConfigurationError(
                    f"Built-in type definition is unavailable for {entry_type!r}"
                )
            if metadata.get("type_version") != definition.version:
                raise UnsupportedSchema(
                    f"Entry type_version is unsupported for {entry_type}; "
                    "run schema validate and preview the required migration"
                )
            try:
                definition.validate_attributes(
                    dict.get(metadata, "attributes"),
                    apply_defaults=False,
                )
            except SchemaPackError as exc:
                raise InvalidRequest(str(exc)) from exc
        return None
    if not isinstance(entry_type, str):
        raise ValidationError("Entry type is invalid; run validate")
    try:
        type_definition = registry.require_type(entry_type)
    except SchemaPackError as exc:
        raise ValidationError(
            f"Entry uses an unavailable schema type; run validate: {exc}"
        ) from exc
    if metadata.get("type_version") != type_definition.version:
        raise UnsupportedSchema(
            f"Entry type_version is unsupported for {entry_type}; "
            "run schema validate and preview the required migration"
        )
    try:
        type_definition.validate_attributes(
            metadata.get("attributes"),
            apply_defaults=False,
        )
    except SchemaPackError as exc:
        raise ValidationError(
            f"Entry attributes are invalid; run validate: {exc}"
        ) from exc
    return type_definition

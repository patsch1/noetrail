#!/usr/bin/env python3
"""Reading Obsidian's on-disk conventions: properties, wikilinks, tags.

Everything here is pure text analysis. It never touches the filesystem, so a
wikilink target -- which is content an import has no reason to trust -- can
only ever be matched against the file list the importer already scanned, and
never used to open a path.

Sources for the conventions implemented here:

- Internal links ``[[Note]]``, ``[[Note|display]]``, ``[[Note#Heading]]``,
  ``[[Note#^block]]``, embeds ``![[...]]``, and "links to file formats other
  than Markdown needs to include a file extension":
  https://obsidian.md/help/Linking+notes+and+files/Internal+links
- Properties are "stored in YAML format at the top of the file"; ``tags``,
  ``aliases`` and ``cssclasses`` are list-typed defaults:
  https://obsidian.md/help/Editing+and+formatting/Properties
- Tag characters, the "at least one non-numerical character" rule, nested
  tags with ``/``, and case-insensitivity:
  https://obsidian.md/help/Editing+and+formatting/Tags
- The vault configuration folder is ``.obsidian``:
  https://obsidian.md/help/Files+and+folders/Configuration+folder
- Canvas files use the ``.canvas`` extension and hold JSON:
  https://jsoncanvas.org/
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from noetrail.constants import (
    MAX_OBSIDIAN_PROPERTY_CHARS,
    MAX_OBSIDIAN_PROPERTY_ITEMS,
    MAX_OBSIDIAN_PROPERTY_KEYS,
    MAX_VAULT_IMPORT_FRONTMATTER_LINES,
    MAX_VAULT_IMPORT_LINE_CHARS,
    MAX_WIKILINK_TARGET_CHARS,
)

_PROPERTY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_FRONTMATTER_DELIMITER = re.compile(r"^(?:---|\.\.\.)\s*$")
_LIST_ITEM = re.compile(r"^(\s*)-(?:\s+(.*))?$")
_INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NUMBER = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$")

# A YAML construct this parser deliberately does not implement. Accepting any
# of them would mean either guessing at the value (an alias that resolves to
# something the reader cannot see next to it) or growing a second, weaker YAML
# implementation next to `schema.parse_restricted_yaml`.
_REFUSED_VALUE_PREFIXES = ("&", "*", "!", "|", ">", "{", "?", "@", "`")

_WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]{1,1024})\]\]")
# A tag is preceded by a character that cannot be part of a word, a nested-tag
# separator, or another hash. That is what keeps `https://host/page#anchor` and
# a Markdown `# Heading` from being read as tags.
_INLINE_TAG = re.compile(r"(?<![\w/#\-])#([\w/\-]{1,120})")
_TAG_BODY = re.compile(r"^[\w/\-][\w/\-]*$")

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")

_TEMPLATER = re.compile(r"<%[-_=*+~]?.*?%>", re.DOTALL)
_CORE_TEMPLATE = re.compile(r"\{\{\s*(date|time|title)\b[^}]*\}\}")
_INLINE_DATAVIEW = re.compile(r"`\$?=[^`\n]+`")

IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".avif"}
)


class ObsidianFrontmatterError(ValueError):
    """The property block uses YAML this importer will not guess at."""

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.reason = message
        self.line = line


@dataclass(frozen=True)
class Wikilink:
    """One ``[[target]]`` or ``![[target]]`` occurrence."""

    embed: bool
    target: str
    subpath: str
    display: str


@dataclass
class LossyConstructs:
    """Syntax that is preserved as text but carries no meaning after import."""

    dataview: int = 0
    templater: int = 0
    core_templates: int = 0


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split an Obsidian property block from the note body.

    Obsidian only recognises a property block that opens on the very first
    line, so a `---` further down is a horizontal rule and stays in the body.
    """

    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if _FRONTMATTER_DELIMITER.fullmatch(lines[index].rstrip()):
            block = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return block, body.lstrip("\n")
    # An unterminated block is not frontmatter; Obsidian shows it as text.
    return None, text


def _scalar(value: str, line: int) -> object:
    stripped = value.strip()
    if not stripped or stripped == "~":
        return None
    if stripped.startswith(_REFUSED_VALUE_PREFIXES) or stripped.startswith("<<"):
        raise ObsidianFrontmatterError(
            "YAML anchors, aliases, tags, merge keys, and block scalars are "
            "not supported",
            line,
        )
    if any(ord(character) < 32 for character in stripped):
        raise ObsidianFrontmatterError("control characters are not supported", line)
    if len(stripped) > MAX_OBSIDIAN_PROPERTY_CHARS:
        raise ObsidianFrontmatterError(
            f"value is longer than {MAX_OBSIDIAN_PROPERTY_CHARS} characters",
            line,
        )
    if stripped.startswith('"') or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    if stripped.startswith("'") and stripped.endswith("'") and len(stripped) > 1:
        return stripped[1:-1].replace("''", "'")
    if stripped.startswith('"') and stripped.endswith('"') and len(stripped) > 1:
        return stripped[1:-1]
    lowered = stripped.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered == "null":
        return None
    if _INTEGER.fullmatch(stripped):
        return int(stripped)
    if _NUMBER.fullmatch(stripped):
        return float(stripped)
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        return [_scalar(item, line) for item in inner.split(",")]
    return stripped


def parse_properties(block: str) -> dict[str, object]:
    """Parse the narrow YAML subset real Obsidian property blocks use.

    Deliberately a separate implementation from `schema.parse_restricted_yaml`
    rather than a relaxation of it. That parser guards trusted install-time
    configuration and may not learn block sequences or unquoted scalars; this
    one reads untrusted foreign data and needs both. The refusals are the
    same, and everything they cover is reported rather than guessed at.
    """

    lines = block.splitlines()
    if len(lines) > MAX_VAULT_IMPORT_FRONTMATTER_LINES:
        raise ObsidianFrontmatterError(
            f"property block has more than {MAX_VAULT_IMPORT_FRONTMATTER_LINES} "
            "lines",
            len(lines),
        )
    properties: dict[str, object] = {}
    pending: str | None = None
    items: list[object] = []

    def close() -> None:
        nonlocal pending, items
        if pending is not None:
            properties[pending] = items
        pending, items = None, []

    for number, raw_line in enumerate(lines, start=1):
        if len(raw_line) > MAX_VAULT_IMPORT_LINE_CHARS:
            raise ObsidianFrontmatterError("line is too long", number)
        if "\t" in raw_line:
            raise ObsidianFrontmatterError("tabs are not supported", number)
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        item = _LIST_ITEM.match(raw_line)
        if item is not None:
            if pending is None:
                raise ObsidianFrontmatterError(
                    "list item without a property name", number
                )
            if len(items) >= MAX_OBSIDIAN_PROPERTY_ITEMS:
                raise ObsidianFrontmatterError(
                    f"property has more than {MAX_OBSIDIAN_PROPERTY_ITEMS} items",
                    number,
                )
            items.append(_scalar(item.group(2) or "", number))
            continue
        if raw_line[:1].isspace():
            raise ObsidianFrontmatterError(
                "nested mappings are not supported", number
            )
        if ":" not in raw_line:
            raise ObsidianFrontmatterError("expected 'name: value'", number)
        close()
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not _PROPERTY_KEY.fullmatch(key):
            raise ObsidianFrontmatterError(f"invalid property name {key!r}", number)
        if key in properties:
            raise ObsidianFrontmatterError(f"duplicate property {key!r}", number)
        if len(properties) >= MAX_OBSIDIAN_PROPERTY_KEYS:
            raise ObsidianFrontmatterError(
                f"more than {MAX_OBSIDIAN_PROPERTY_KEYS} properties", number
            )
        if not raw_value.strip():
            pending = key
            items = []
            continue
        properties[key] = _scalar(raw_value, number)
    close()
    return properties


def strip_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans, keeping line numbers.

    Obsidian does not resolve links or tags inside code, and a Dataview
    example that happens to contain `[[Note]]` must not create a relation.
    """

    result: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match is not None:
                fence = match.group(1)[0] * 3
                result.append("")
                continue
            result.append(_INLINE_CODE.sub(" ", line))
            continue
        if match is not None and match.group(1).startswith(fence):
            fence = None
        result.append("")
    return "\n".join(result)


def wikilinks(body: str) -> list[Wikilink]:
    """Every wikilink in the note body, in order, code spans excluded."""

    found: list[Wikilink] = []
    for match in _WIKILINK.finditer(strip_code(body)):
        inner = match.group(2)
        target_part, _, display = inner.partition("|")
        target, _, subpath = target_part.partition("#")
        found.append(
            Wikilink(
                embed=match.group(1) == "!",
                target=target.strip()[:MAX_WIKILINK_TARGET_CHARS],
                subpath=subpath.strip()[:MAX_WIKILINK_TARGET_CHARS],
                display=display.strip()[:MAX_WIKILINK_TARGET_CHARS],
            )
        )
    return found


def normalize_tag(value: object) -> str | None:
    """Return a storable tag, or None when Obsidian would not accept it.

    "Tags must contain at least one non-numerical character" and cannot
    contain spaces; a leading `#` is the inline spelling of the same tag.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip().lstrip("#").strip("/")
    if not text or len(text) > 120:
        return None
    if not _TAG_BODY.fullmatch(text):
        return None
    if text.replace("/", "").replace("-", "").replace("_", "").isdigit():
        return None
    return text


def inline_tags(body: str) -> list[str]:
    """Tags written as `#tag` in the note text, code spans excluded."""

    found: list[str] = []
    for match in _INLINE_TAG.finditer(strip_code(body)):
        tag = normalize_tag(match.group(1))
        if tag is not None:
            found.append(tag)
    return found


def lossy_constructs(text: str) -> LossyConstructs:
    """Count syntax that survives as text but loses its behaviour on import."""

    report = LossyConstructs()
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match is not None:
                fence = match.group(1)[0] * 3
                if match.group(2).casefold() in {"dataview", "dataviewjs"}:
                    report.dataview += 1
            continue
        if match is not None and match.group(1).startswith(fence):
            fence = None
    report.dataview += len(_INLINE_DATAVIEW.findall(text))
    report.templater = len(_TEMPLATER.findall(text))
    report.core_templates = len(_CORE_TEMPLATE.findall(text))
    return report


def first_heading(text: str) -> str | None:
    """The first level-one ATX heading, which Obsidian shows as the title."""

    for line in text.splitlines():
        match = re.fullmatch(r"#\s+(.+?)\s*", line)
        if match:
            title = match.group(1).strip().strip("#").strip()
            if title:
                return title[:500]
    return None

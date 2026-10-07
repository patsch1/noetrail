#!/usr/bin/env python3
"""Reading Basic Memory's on-disk Markdown conventions.

Basic Memory (AGPL-3.0, `basicmachines-co/basic-memory`) stores knowledge as
Markdown files plus a SQLite index. The index is derived, not authoritative --
"all knowledge is represented in standard Markdown files and the database
serves as a secondary index" -- so this importer reads the files only and
never opens the database.

The format, quoted from the project's own documentation:

- Frontmatter carries ``title``, ``type``, ``permalink`` and ``tags``:
  https://docs.basicmemory.com/concepts/knowledge-format
- Observations are list items with a bracketed category,
  ``- [category] content #optional-tags (optional context)``.
- Relations are list items of the form ``- relation_type [[Target]]``, and
  "links can reference notes not yet created".
- Notes live in ``~/basic-memory`` by default:
  https://github.com/basicmachines-co/basic-memory (README)

Nothing beyond those documented shapes is inferred. A line inside an
``## Observations`` or ``## Relations`` section that does not match is left in
the body unchanged and counted in the import report.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from noetrail.constants import MAX_WIKILINK_TARGET_CHARS
from noetrail.obsidian import normalize_tag, strip_code

_SECTION = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_OBSERVATION = re.compile(r"^\s*[-*]\s+\[([^\]\n]{1,64})\]\s*(.+?)\s*$")
_RELATION = re.compile(
    r"^\s*[-*]\s+([A-Za-z][A-Za-z0-9_ -]{0,63}?)\s+\[\[([^\[\]\n]{1,1024})\]\]"
    r"\s*$"
)

OBSERVATIONS_HEADING = "observations"
RELATIONS_HEADING = "relations"


@dataclass(frozen=True)
class Observation:
    """One ``- [category] content`` fact."""

    category: str
    content: str


@dataclass(frozen=True)
class TypedRelation:
    """One ``- relation_type [[Target]]`` edge, before predicate mapping."""

    relation_type: str
    target: str
    subpath: str


@dataclass
class Sections:
    """What a Basic Memory note declares, plus what did not parse."""

    observations: list[Observation]
    relations: list[TypedRelation]
    unparsed_lines: int


def parse_sections(body: str) -> Sections:
    """Read the ``## Observations`` and ``## Relations`` sections.

    Section membership is decided by the nearest preceding heading, so a
    ``- foo [[Bar]]`` line in ordinary prose is not turned into a typed edge
    and an observation-shaped bullet outside the section stays prose.
    """

    observations: list[Observation] = []
    relations: list[TypedRelation] = []
    unparsed = 0
    section = ""
    for line in strip_code(body).splitlines():
        heading = _SECTION.fullmatch(line)
        if heading is not None:
            section = heading.group(1).strip().casefold()
            continue
        if section == OBSERVATIONS_HEADING:
            match = _OBSERVATION.fullmatch(line)
            if match is not None:
                observations.append(
                    Observation(
                        category=match.group(1).strip()[:64],
                        content=match.group(2)[:1000],
                    )
                )
            elif line.strip().startswith(("-", "*")):
                unparsed += 1
            continue
        if section == RELATIONS_HEADING:
            match = _RELATION.fullmatch(line)
            if match is not None:
                target, _, subpath = match.group(2).partition("#")
                relations.append(
                    TypedRelation(
                        relation_type=match.group(1).strip().casefold(),
                        target=target.strip()[:MAX_WIKILINK_TARGET_CHARS],
                        subpath=subpath.strip()[:MAX_WIKILINK_TARGET_CHARS],
                    )
                )
            elif line.strip().startswith(("-", "*")):
                unparsed += 1
    return Sections(
        observations=observations,
        relations=relations,
        unparsed_lines=unparsed,
    )


def observation_tags(observations: list[Observation]) -> list[str]:
    """The `#tags` an observation line carries, in document order."""

    found: list[str] = []
    for observation in observations:
        for raw in re.findall(r"(?<![\w/#])#([\w/-]{1,120})", observation.content):
            tag = normalize_tag(raw)
            if tag is not None:
                found.append(tag)
    return found

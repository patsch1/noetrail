"""Bounded search evidence with the same fields and provenance as retrieval."""

import json

from noetrail.body import body_sections
from noetrail.constants import WEB_CONTENT_BEGIN, WEB_CONTENT_END
from noetrail.deadline import check_deadline
from noetrail.frontmatter import entry_type_name
from noetrail.provenance import entry_provenance
from noetrail.schema import SchemaRegistry
from noetrail.search import RANK_SUBSTRING, TOKEN_PATTERN, tokenize


def _match_start(text: str, query: str, terms: set[str]) -> int | None:
    literal = text.casefold().find(query.casefold())
    if literal >= 0:
        folded_position = 0
        for position, character in enumerate(text):
            if folded_position + len(character.casefold()) > literal:
                return position
            folded_position += len(character.casefold())
    for match in TOKEN_PATTERN.finditer(text):
        check_deadline()
        if terms.intersection(tokenize(match.group())):
            return match.start()
    return None


def match_evidence(
    metadata: dict[str, object],
    body: str,
    registry: SchemaRegistry,
    query: str,
    rank: str,
    headings: dict[str, str],
    *,
    matched_terms: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    """At most five excerpts of 240 characters; never infer an origin."""
    if not query:
        return []
    terms = set(tokenize(query)) if rank != RANK_SUBSTRING else set()
    terms.update(matched_terms)
    origins = entry_provenance(metadata)
    fields: list[tuple[str, str, str, int | None]] = []
    for name in ("title", "aliases", "tags"):
        if name in metadata:
            fields.append(
                (name, str(metadata[name]), origins.get(name, "unknown"), None)
            )
    definition = registry.types.get(entry_type_name(metadata) or "")
    attributes = metadata.get("attributes", {})
    if definition is not None:
        for name, field in sorted(definition.fields.items()):
            if not field.searchable:
                continue
            value = attributes.get(name) if isinstance(attributes, dict) else None
            if value is None:
                value = metadata.get(name)
            if value is not None:
                fields.append(
                    (
                        f"attributes.{name}",
                        json.dumps(value, ensure_ascii=False),
                        origins.get(name, "unknown"),
                        None,
                    )
                )
    lines = body.splitlines()
    for section in body_sections(body, headings):
        first, last = int(str(section["first_line"])), int(str(section["last_line"]))
        for number in range(first, last + 1):
            check_deadline()
            line = lines[number - 1]
            if line.strip() not in (WEB_CONTENT_BEGIN, WEB_CONTENT_END):
                fields.append(("body", line, str(section["origin"]), number))
    evidence: list[dict[str, object]] = []
    for field_name, text, origin, line_number in fields:
        check_deadline()
        position = _match_start(text, query, terms)
        if position is None:
            continue
        start = max(0, position - 60)
        excerpt = text[start : start + 240]
        item: dict[str, object] = {
            "field": field_name,
            "excerpt": excerpt,
            "origin": origin,
            "truncated": start > 0 or start + len(excerpt) < len(text),
        }
        if line_number is not None:
            item["body_line"] = line_number
        evidence.append(item)
        if len(evidence) == 5:
            break
    return evidence

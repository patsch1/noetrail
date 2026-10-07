#!/usr/bin/env python3
"""Configurable body headings and the sections a Markdown body is cut into."""

from __future__ import annotations

from pathlib import Path

from noetrail.constants import WEB_CONTENT_BEGIN, WEB_CONTENT_END
from noetrail.deadline import check_deadline
from noetrail.errors import (
    ConfigurationError,
)
from noetrail.layout import (
    NoetrailLayout,
)

# Section headings written into entry bodies. They are configurable because
# they are user-visible prose inside personal notes, and existing vaults must
# not be rewritten just because the shipped default language changed.
DEFAULT_BODY_HEADINGS: dict[str, str] = {
    "link": "Link",
    "personal_note": "Personal note",
    "generated_summary": "Generated summary",
    "personal_rating": "Personal rating",
    "ingredients": "Ingredients",
    "preparation": "Preparation",
    "personal_notes": "Personal notes",
}


# Headings written by development versions before 0.10.0a1, which defaulted to German.
# Detection still accepts them so no content migration is required.
LEGACY_BODY_HEADINGS: dict[str, tuple[str, ...]] = {
    "link": ("Link",),
    "personal_note": ("Persönliche Notiz",),
    "generated_summary": ("Automatisch erzeugte Zusammenfassung",),
    "personal_rating": ("Persönliche Bewertung",),
    "ingredients": ("Zutaten",),
    "preparation": ("Zubereitung",),
    "personal_notes": ("Persönliche Hinweise",),
}


def heading_for(headings: dict[str, str] | None, key: str) -> str:
    if headings and key in headings:
        return headings[key]
    return DEFAULT_BODY_HEADINGS[key]


def read_body_headings(path: Path) -> dict[str, str]:
    """Read the ``body_headings`` mapping out of one configuration file.

    Only that one block is interpreted; the rest of the file is documentation
    for the vault layout and is deliberately left alone.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    headings: dict[str, str] = {}
    inside = False
    for number, line in enumerate(text.splitlines(), start=1):
        check_deadline()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside = line.rstrip() == "body_headings:"
            continue
        if not inside:
            continue
        entry = line.strip()
        if ":" not in entry:
            raise ConfigurationError(f"{path}:{number}: expected key: value")
        key, raw_value = entry.split(":", 1)
        key = key.strip()
        if key not in DEFAULT_BODY_HEADINGS:
            raise ConfigurationError(f"{path}:{number}: unknown body heading {key!r}")
        value = raw_value.strip().strip('"').strip("'").strip()
        if not value or "\n" in value or value.startswith("#"):
            raise ConfigurationError(
                f"{path}:{number}: body heading {key!r} must be a short string"
            )
        headings[key] = value
    return headings


def load_body_headings(layout: NoetrailLayout) -> dict[str, str]:
    """Resolve heading names from the built-in defaults and instance overrides."""

    headings = dict(DEFAULT_BODY_HEADINGS)
    headings.update(read_body_headings(layout.builtins_root / "config.yaml"))
    headings.update(read_body_headings(layout.config_root / "config.yaml"))
    return headings


def heading_variants(headings: dict[str, str] | None, key: str) -> set[str]:
    """Return every spelling of one section heading that has ever been written.

    The configured name, the shipped default, and the pre-0.10.0a1 German
    names. A vault that overrode a heading, or that predates the rename, must
    still be readable without a content migration.
    """

    return {
        f"## {heading_for(headings, key)}".casefold(),
        f"## {DEFAULT_BODY_HEADINGS[key]}".casefold(),
        *(
            f"## {legacy}".casefold()
            for legacy in LEGACY_BODY_HEADINGS[key]
        ),
    }


def body_sections(
    body: str, headings: dict[str, str] | None = None
) -> list[dict[str, object]]:
    """Split a body into runs of one heading and one origin.

    Two signals decide the origin, in this order:

    * the ``<!-- noetrail:web-content -->`` fence, written by the one command
      that ingests fetched text. An unclosed fence marks everything after it,
      because a truncated or hand-edited file must not turn fetched text back
      into trusted text.
    * the ``## Generated summary`` heading, which is all that entries written
      before schema 10 have. It is a weak signal -- the heading is prose and a
      user can type it -- so `marked` reports which of the two applied, and it
      is consulted only in bodies that carry no fence at all. Mixing the two
      re-marked text the user appended *after* a fenced summary, because that
      text still sits under the fetched section's heading.

    Everything else is reported as ``user``. That is the honest default for a
    local-first vault whose only network ingest path writes the fence, and it
    is a default, not a guarantee: the value of this function is that a client
    can see the boundary without parsing Markdown, not that the boundary is
    enforced anywhere.
    """

    lines = body.splitlines()
    summary_headings = (
        set()
        if WEB_CONTENT_BEGIN in body
        else heading_variants(headings, "generated_summary")
    )
    sections: list[dict[str, object]] = []
    heading: str | None = None
    inside = False
    # A fence opens one line above its heading, so the opening line has to join
    # the section it introduces instead of closing the previous one -- marking
    # the personal note above it as web would be exactly the wrong answer.
    pending_start: int | None = None

    for number, line in enumerate(lines, start=1):
        check_deadline()
        stripped = line.strip()
        if stripped == WEB_CONTENT_BEGIN:
            inside = True
            if pending_start is None:
                pending_start = number
            continue
        if stripped == WEB_CONTENT_END:
            inside = False
            # `pending_start` still set means the fence held no content at all;
            # there is no section of its own to extend, and extending the one
            # before it would relabel unfenced text.
            if pending_start is None and sections and sections[-1]["marked"]:
                sections[-1]["last_line"] = number
            pending_start = None
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
        by_heading = (
            heading is not None
            and f"## {heading}".casefold() in summary_headings
        )
        origin = "web" if inside or by_heading else "user"
        if (
            sections
            and sections[-1]["heading"] == heading
            and sections[-1]["origin"] == origin
            and sections[-1]["marked"] == inside
        ):
            sections[-1]["last_line"] = number
            continue
        sections.append(
            {
                "heading": heading,
                "origin": origin,
                "marked": inside,
                "first_line": number if pending_start is None else pending_start,
                "last_line": number,
            }
        )
        pending_start = None
    return sections


def body_has_web_content(
    body: str, headings: dict[str, str] | None = None
) -> bool:
    """Cheap variant of `body_sections` for the search loop.

    `search` evaluates this once per match, before the result page is sliced,
    so it must not walk the body twice.
    """

    if WEB_CONTENT_BEGIN in body:
        return True
    if "## " not in body:
        return False
    summary_headings = heading_variants(headings, "generated_summary")
    return any(
        line.strip().casefold() in summary_headings
        for line in body.splitlines()
    )


def fence_web_content(text: str) -> list[str]:
    """Wrap one block of fetched text in the machine-readable fence."""

    return [WEB_CONTENT_BEGIN, *text.splitlines(), WEB_CONTENT_END]

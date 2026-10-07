#!/usr/bin/env python3
"""Canonical forms for titles, tags, slugs, URLs, timestamps."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from noetrail.constants import (
    MAX_ALIAS_CHARS,
    MAX_ALIASES_PER_ENTRY,
    TRACKING_QUERY_KEYS,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def tag_key(value: str) -> str:
    """Return the identity key for a tag.

    Two tags are the same tag when their keys match. The key is only ever used
    for comparison and de-duplication; the value written to an entry keeps the
    spelling the author chose.
    """
    return value.strip().casefold()


def name_key(value: str) -> str:
    """Return the Unicode-normalized identity key for a title or alias."""

    return unicodedata.normalize("NFKC", value.strip()).casefold()


def normalize_alias_list(values: Iterable[str], title: str) -> list[str]:
    """Validate and canonicalize alternative names without changing spelling."""

    unique: dict[str, str] = {}
    title_identity = name_key(title)
    for value in values:
        stripped = value.strip()
        if not stripped:
            raise ValueError("aliases must not contain empty values")
        if len(stripped) > MAX_ALIAS_CHARS:
            raise ValueError(
                f"aliases must not exceed {MAX_ALIAS_CHARS} characters"
            )
        identity = name_key(stripped)
        if identity == title_identity:
            raise ValueError("an alias must differ from the title")
        if identity in unique:
            raise ValueError("aliases must be unique")
        unique[identity] = stripped
    if len(unique) > MAX_ALIASES_PER_ENTRY:
        raise ValueError(
            f"aliases must contain at most {MAX_ALIASES_PER_ENTRY} values"
        )
    return [unique[key] for key in sorted(unique)]


def normalize_tag_list(values: Iterable[str]) -> list[str]:
    """De-duplicate tags case-insensitively, keeping the first spelling seen."""
    unique: dict[str, str] = {}
    for value in values:
        stripped = value.strip()
        if stripped:
            unique.setdefault(tag_key(stripped), stripped)
    return sorted(unique.values())


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug[:64] or "entry"


def infer_title(text: str) -> str:
    line = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        "Untitled",
    )
    line = re.sub(r"^#+\s*", "", line)
    sentence = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0]
    return sentence[:80].rstrip()


def canonicalize_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a host")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not supported")

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    if port and not default_port:
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    query_items = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    query = urlencode(sorted(query_items))
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))

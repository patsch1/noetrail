#!/usr/bin/env python3
"""Loading, indexing, and finding entries in one vault scan."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import functools
from pathlib import Path

from noetrail.deadline import check_deadline
from noetrail.errors import (
    Conflict,
    NotFound,
)
from noetrail.frontmatter import entry_type_name, parse_frontmatter, runtime_metadata
from noetrail.store import entry_paths, trash_paths


def load_entries_with_diagnostics(
    root: Path,
) -> tuple[list[tuple[Path, dict[str, object], str]], list[str]]:
    """Load every active entry and report the ones that could not be read.

    Callers that present a result set to an agent need the second return
    value: silently dropping an entry makes "no match" and "could not read
    this file" indistinguishable, and an agent that reads `total: 0` as proof
    of absence will happily create a duplicate.
    """
    entries = []
    unreadable: list[str] = []
    for path in entry_paths(root):
        check_deadline()
        try:
            metadata, body = parse_frontmatter(path)
        except (OSError, UnicodeError, ValueError):
            # A UTF-8 BOM, a lost ACL after a restore, a stale network mount.
            unreadable.append(str(path.relative_to(root)))
            continue
        entries.append((path, runtime_metadata(metadata), body))
    return entries, unreadable


def load_entries(root: Path) -> list[tuple[Path, dict[str, object], str]]:
    entries, _ = load_entries_with_diagnostics(root)
    return entries


def load_trashed_entries(root: Path) -> list[tuple[Path, dict[str, object], str]]:
    entries = []
    for path in trash_paths(root):
        check_deadline()
        try:
            metadata, body = parse_frontmatter(path)
        except (OSError, UnicodeError, ValueError):
            continue
        entries.append((path, runtime_metadata(metadata), body))
    return entries


LoadedEntry = tuple[Path, dict[str, object], str]


class VaultIndex:
    """One vault scan per command, shared by every lookup inside it.

    Measured at 10 000 entries before this existed: `find_entry`,
    `duplicate_titles`, `duplicate_bookmarks`, `duplicate_recipes` and
    `validate_relation_objects` each called `load_entries` for themselves and
    no caller shared the result, so `tag` cost one full scan (521 ms),
    `capture` two (979 ms) and `relate` three (980 ms per direction). A
    `find_entry` miss then added a fourth scan only to count damaged files.

    Nothing is written to disk. The index lives for the duration of one
    command, inside the vault lock the command already holds, and is rebuilt
    from Markdown next time -- so "the Markdown is the truth" is unchanged and
    there is no cache to invalidate between processes.

    It is a snapshot: a handler that mutates entries and then queries the index
    again would read pre-mutation state. Every current handler does all of its
    lookups before its write, which is also what the revision check requires.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @functools.cached_property
    def _scan(self) -> tuple[list[LoadedEntry], list[str]]:
        return load_entries_with_diagnostics(self.root)

    @property
    def entries(self) -> list[LoadedEntry]:
        return self._scan[0]

    @property
    def unreadable(self) -> list[str]:
        """Vault-relative paths whose frontmatter could not be parsed."""

        return self._scan[1]

    @functools.cached_property
    def by_id(self) -> dict[str, list[LoadedEntry]]:
        # A list, not a single entry: a duplicated ID has to stay visible so
        # `find_entry` can refuse to edit it instead of picking one at random.
        index: dict[str, list[LoadedEntry]] = {}
        for entry in self.entries:
            check_deadline()
            entry_id = entry[1].get("id")
            if isinstance(entry_id, str):
                index.setdefault(entry_id, []).append(entry)
        return index

    @functools.cached_property
    def ids(self) -> set[str]:
        return set(self.by_id)

    @functools.cached_property
    def by_type_title(self) -> dict[tuple[str, str], list[Path]]:
        index: dict[tuple[str, str], list[Path]] = {}
        for path, metadata, _ in self.entries:
            check_deadline()
            entry_type = entry_type_name(metadata)
            if entry_type is None:
                continue
            title = str(metadata.get("title", "")).casefold().strip()
            index.setdefault((entry_type, title), []).append(path)
        return index

    @functools.cached_property
    def by_canonical_url(self) -> dict[str, list[Path]]:
        return self._by_field("bookmark", "canonical_url")

    @functools.cached_property
    def by_source_url(self) -> dict[str, list[Path]]:
        return self._by_field("recipe", "source_url")

    def _by_field(self, entry_type: str, field: str) -> dict[str, list[Path]]:
        index: dict[str, list[Path]] = {}
        for path, metadata, _ in self.entries:
            check_deadline()
            if metadata.get("type") != entry_type:
                continue
            value = metadata.get(field)
            if isinstance(value, str):
                index.setdefault(value, []).append(path)
        return index


def timestamp_sort_key(item: dict[str, object], field: str) -> float:
    """Return a sortable instant for an ISO-8601 timestamp field.

    `now_iso()` writes local time with an offset, not UTC, so sorting the raw
    strings lexicographically orders by wall-clock reading rather than by
    instant. Two entries written either side of a flight or a DST change then
    come back in the wrong order. Values that are missing, unparseable or
    naive sort last, which keeps a damaged entry from jumping to the top.
    """
    value = item.get(field)
    if not isinstance(value, str):
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        return float("-inf")
    return parsed.timestamp()


def unreadable_entry_count(paths: Iterable[Path]) -> int:
    """Count managed files whose frontmatter cannot be parsed."""

    unreadable = 0
    for path in paths:
        check_deadline()
        try:
            parse_frontmatter(path)
        except (OSError, UnicodeError, ValueError):
            unreadable += 1
    return unreadable


def find_entry(index: VaultIndex, entry_id: str) -> LoadedEntry:
    matches = index.by_id.get(entry_id, [])
    if not matches:
        # A damaged entry is skipped while loading, so distinguish it from a
        # genuinely unknown ID instead of reporting a misleading absence. The
        # count comes out of the scan that already ran; re-walking the vault
        # here was a fourth full scan on the miss path.
        damaged = len(index.unreadable)
        if damaged:
            raise NotFound(
                f"Entry does not exist: {entry_id}. {damaged} vault file(s) "
                "could not be parsed; run `noetrail validate`"
            )
        raise NotFound(f"Entry does not exist: {entry_id}")
    if len(matches) > 1:
        raise Conflict(f"Entry ID is duplicated and cannot be edited: {entry_id}")
    return matches[0]


def find_trashed_entry(
    root: Path, entry_id: str
) -> tuple[Path, dict[str, object], str]:
    matches = [
        (path, metadata, body)
        for path, metadata, body in load_trashed_entries(root)
        if metadata.get("id") == entry_id
    ]
    if not matches:
        raise NotFound(f"Trashed entry does not exist: {entry_id}")
    if len(matches) > 1:
        raise Conflict(
            f"Trashed entry ID is duplicated and cannot be restored: {entry_id}"
        )
    return matches[0]


def duplicate_titles(
    index: VaultIndex, entry_type: str, title: str
) -> list[Path]:
    return index.by_type_title.get((entry_type, title.casefold().strip()), [])


def duplicate_bookmarks(index: VaultIndex, canonical_url: str) -> list[Path]:
    return index.by_canonical_url.get(canonical_url, [])


def duplicate_recipes(index: VaultIndex, source_url: str) -> list[Path]:
    return index.by_source_url.get(source_url, [])

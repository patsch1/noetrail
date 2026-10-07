#!/usr/bin/env python3
"""The persisted BM25 index: a cache of the vault, never a copy of record.

Everything here is derived. The vault's Markdown is the only thing that
decides what an entry says; this file holds a lexical index of that text so a
query does not have to read fifty thousand files to answer. Three rules follow
from that and are implemented rather than merely stated:

*Detectable.* Every read verifies the index against the vault before using it,
by comparing a digest over ``(vault-relative path, mtime_ns, size)`` for every
Markdown file plus a fingerprint of the schema fields that were indexed. A
mismatch means the index is not used at all -- search falls back to scanning
the vault. There is no code path that returns a hit from an index that does
not match the files.

*Deletable.* ``noetrail index drop`` removes it, and so does ``rm -rf``. Search
keeps working, slower. Nothing in a backup or a restore refers to it, which is
why it lives under the instance config root and not under the data root.

*Whole or absent.* The file is written to a temporary name, fsynced, and
renamed into place, and its payload carries a SHA-256 that is verified before
a single posting is read. A truncated or edited file is discarded and rebuilt;
it is never partially believed.

Why ``(path, mtime_ns, size)`` and not the ``sha256:`` revision the entry
commands already use: the revision hash is computed by reading the whole file,
so verifying freshness that way costs exactly as much as the scan the index
exists to avoid. A stat is roughly an order of magnitude cheaper and is what
the check has to afford on every query. The cost is the documented blind spot
of every mtime-based cache -- a rewrite that lands in the same modification
timestamp *and* keeps the byte length identical is invisible -- so
``noetrail index status --verify`` re-derives the index from the files and
compares content, and ``noetrail doctor`` reports the state.

The on-disk layout is one header line of JSON followed by fixed-width binary
sections. JSON alone was measured at roughly 0.64 s to parse a fifty-thousand
entry index against 0.02 s for the same data as ``array`` sections, and the
whole point is to be faster than the 1.4 s scan it replaces. The header stays
JSON so the interesting facts -- what it indexed, when, against which vault --
are readable with ``head -1``.
"""

from __future__ import annotations

import array
import bisect
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import TYPE_CHECKING

from noetrail.deadline import check_deadline
from noetrail.errors import StorageError
from noetrail.frontmatter import parse_frontmatter, runtime_metadata
from noetrail.search import registry_fingerprint, searchable_text, term_frequencies

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from collections.abc import Sequence

    from noetrail.layout import NoetrailLayout
    from noetrail.schema import SchemaRegistry

INDEX_FORMAT = "noetrail-bm25-1"
INDEX_FILENAME = "bm25.idx"

# A free document slot. Slots are reused rather than compacted, so removing an
# entry never has to renumber the ordinals inside every posting list.
FREE_SLOT = -1

_ORDINAL = "i"
_COUNTER = "i"
_OFFSET = "q"

_SECTIONS: tuple[tuple[str, str], ...] = (
    ("postings_documents", _ORDINAL),
    ("postings_frequencies", _COUNTER),
    ("postings_offsets", _OFFSET),
    ("document_lengths", _ORDINAL),
    ("document_mtime_ns", _OFFSET),
    ("document_size", _OFFSET),
    ("forward_terms", _ORDINAL),
    ("forward_offsets", _OFFSET),
    ("strings", "B"),
    ("string_offsets", _OFFSET),
    ("vocabulary", "B"),
    ("vocabulary_offsets", _OFFSET),
    ("vocabulary_sorted", _ORDINAL),
)


class IndexUnusable(Exception):
    """The stored index cannot be read or does not match this vault.

    Never propagated to a caller: every raise site turns it into a fallback to
    the vault scan. It exists so the reasons stay distinguishable in the state
    string that ``doctor`` and ``index status`` report.
    """

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(detail)
        self.state = state
        self.detail = detail


def index_directory(layout: NoetrailLayout) -> Path:
    """Where this vault's index lives, below the instance config root.

    Not below the data root: a backup of the data root must not contain a
    derived file that could be restored and believed. The per-vault
    subdirectory is a digest of the resolved data root, because one config
    root can serve several vaults and two of them must not overwrite each
    other's cache.
    """

    digest = hashlib.sha256(
        str(layout.data_root).encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]
    return layout.config_root / "index" / digest


def index_path(layout: NoetrailLayout) -> Path:
    return index_directory(layout) / INDEX_FILENAME


# ---------------------------------------------------------------------------
# Vault signatures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultSignature:
    """What the vault looks like from the outside, without reading content."""

    signatures: dict[str, tuple[int, int]]
    digest: str


def _walk_entries(directory: str, root: str, found: dict[str, tuple[int, int]]) -> None:
    """Reproduce ``vault.rglob("*.md")`` while reusing the walk's own stat.

    ``entry_paths`` walks the tree and then stats each result again, which is
    two passes over the same inodes; at ten thousand entries that was measured
    at 161 ms against 73 ms for this. The freshness check runs on every indexed
    query, so it is the floor under the latency the index can deliver and worth
    the duplication -- which
    ``tests/test_search_index.py::test_the_signature_walk_covers_exactly_the_scanned_files``
    pins against ``entry_paths`` so the two cannot drift apart.

    ``rglob`` recurses into real directories only, never into symlinked ones,
    but does yield anything whose *name* matches -- including a directory. Both
    behaviours are mirrored, because a file the scan sees and the index does
    not is exactly the disagreement this module exists to prevent.
    """

    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        check_deadline()
        if entry.name.endswith(".md"):
            try:
                status = entry.stat()
            except OSError:
                # Vanished between the walk and the stat. The next query walks
                # again and sees a different digest either way.
                continue
            found[os.path.relpath(entry.path, root)] = (
                status.st_mtime_ns,
                status.st_size,
            )
        try:
            recurse = entry.is_dir(follow_symlinks=False)
        except OSError:
            recurse = False
        if recurse:
            _walk_entries(entry.path, root, found)


def scan_signatures(root: Path) -> VaultSignature:
    """Stat every managed entry file and digest the result."""

    signatures: dict[str, tuple[int, int]] = {}
    vault = root / "vault"
    if vault.exists():
        _walk_entries(str(vault), str(root), signatures)
    material = "\n".join(
        f"{relative}\0{mtime}\0{size}"
        for relative, (mtime, size) in sorted(signatures.items())
    )
    return VaultSignature(
        signatures=signatures,
        digest="sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )


# ---------------------------------------------------------------------------
# The mutable working form, used to build and update
# ---------------------------------------------------------------------------


@dataclass
class DocumentSlot:
    path: str
    entry_id: str
    mtime_ns: int
    size: int
    length: int
    terms: list[int] = field(default_factory=list)


class MutableIndex:
    """The form the index takes while it is being built or patched.

    Postings are per-term sorted arrays of document ordinals, so removing one
    document costs a binary search and a memmove per term it contains rather
    than a pass over the whole index. That is what makes an incremental update
    after a single capture proportional to the entry, not to the vault.
    """

    def __init__(self) -> None:
        self.slots: list[DocumentSlot | None] = []
        self.free: list[int] = []
        self.by_path: dict[str, int] = {}
        self.vocabulary: list[str] = []
        self.term_ids: dict[str, int] = {}
        self.postings: list[array.array] = []
        self.frequencies: list[array.array] = []
        self.unreadable: dict[str, tuple[int, int]] = {}
        self.total_length = 0
        self.live = 0

    def _term_id(self, term: str) -> int:
        identifier = self.term_ids.get(term)
        if identifier is None:
            identifier = len(self.vocabulary)
            self.vocabulary.append(term)
            self.term_ids[term] = identifier
            self.postings.append(array.array(_ORDINAL))
            self.frequencies.append(array.array(_COUNTER))
        return identifier

    def remove(self, relative: str) -> None:
        ordinal = self.by_path.pop(relative, None)
        self.unreadable.pop(relative, None)
        if ordinal is None:
            return
        slot = self.slots[ordinal]
        assert slot is not None
        for term_id in slot.terms:
            check_deadline()
            postings = self.postings[term_id]
            position = bisect.bisect_left(postings, ordinal)
            if position < len(postings) and postings[position] == ordinal:
                postings.pop(position)
                self.frequencies[term_id].pop(position)
        self.total_length -= slot.length
        self.live -= 1
        self.slots[ordinal] = None
        self.free.append(ordinal)

    def add(
        self,
        relative: str,
        entry_id: str,
        mtime_ns: int,
        size: int,
        frequencies: Counter[str],
    ) -> None:
        ordinal = self.free.pop() if self.free else len(self.slots)
        if ordinal == len(self.slots):
            self.slots.append(None)
        length = sum(frequencies.values())
        term_ids: list[int] = []
        for term, count in frequencies.items():
            check_deadline()
            term_id = self._term_id(term)
            term_ids.append(term_id)
            postings = self.postings[term_id]
            position = bisect.bisect_left(postings, ordinal)
            postings.insert(position, ordinal)
            self.frequencies[term_id].insert(position, count)
        term_ids.sort()
        self.slots[ordinal] = DocumentSlot(
            path=relative,
            entry_id=entry_id,
            mtime_ns=mtime_ns,
            size=size,
            length=length,
            terms=term_ids,
        )
        self.by_path[relative] = ordinal
        self.total_length += length
        self.live += 1

    def mark_unreadable(self, relative: str, mtime_ns: int, size: int) -> None:
        self.unreadable[relative] = (mtime_ns, size)

    def serialize(
        self,
        *,
        data_root: Path,
        fingerprint: str,
        signature: VaultSignature,
        built_at_ns: int,
    ) -> bytes:
        sections: list[tuple[str, bytes]] = []

        postings_documents = array.array(_ORDINAL)
        postings_frequencies = array.array(_COUNTER)
        postings_offsets = array.array(_OFFSET, [0])
        for term_id in range(len(self.vocabulary)):
            check_deadline()
            postings_documents.extend(self.postings[term_id])
            postings_frequencies.extend(self.frequencies[term_id])
            postings_offsets.append(len(postings_documents))

        lengths = array.array(_ORDINAL)
        mtimes = array.array(_OFFSET)
        sizes = array.array(_OFFSET)
        forward_terms = array.array(_ORDINAL)
        forward_offsets = array.array(_OFFSET, [0])
        strings = bytearray()
        string_offsets = array.array(_OFFSET, [0])
        for slot in self.slots:
            check_deadline()
            if slot is None:
                lengths.append(FREE_SLOT)
                mtimes.append(0)
                sizes.append(0)
            else:
                lengths.append(slot.length)
                mtimes.append(slot.mtime_ns)
                sizes.append(slot.size)
                forward_terms.extend(slot.terms)
                strings.extend(
                    f"{slot.path}\n{slot.entry_id}".encode("utf-8", "surrogatepass")
                )
            forward_offsets.append(len(forward_terms))
            string_offsets.append(len(strings))

        vocabulary = bytearray()
        vocabulary_offsets = array.array(_OFFSET, [0])
        for term in self.vocabulary:
            check_deadline()
            vocabulary.extend(term.encode("utf-8", "surrogatepass"))
            vocabulary_offsets.append(len(vocabulary))
        vocabulary_sorted = array.array(
            _ORDINAL,
            sorted(range(len(self.vocabulary)), key=self.vocabulary.__getitem__),
        )

        for name, value in (
            ("postings_documents", postings_documents),
            ("postings_frequencies", postings_frequencies),
            ("postings_offsets", postings_offsets),
            ("document_lengths", lengths),
            ("document_mtime_ns", mtimes),
            ("document_size", sizes),
            ("forward_terms", forward_terms),
            ("forward_offsets", forward_offsets),
            ("strings", bytes(strings)),
            ("string_offsets", string_offsets),
            ("vocabulary", bytes(vocabulary)),
            ("vocabulary_offsets", vocabulary_offsets),
            ("vocabulary_sorted", vocabulary_sorted),
        ):
            check_deadline()
            sections.append(
                (name, value if isinstance(value, bytes) else value.tobytes())
            )

        payload = b"".join(blob for _, blob in sections)
        header = {
            "format": INDEX_FORMAT,
            "byte_order": sys.byteorder,
            "item_sizes": {
                _ORDINAL: array.array(_ORDINAL).itemsize,
                _OFFSET: array.array(_OFFSET).itemsize,
            },
            "data_root": str(data_root),
            "built_at": _iso(built_at_ns),
            "built_at_ns": built_at_ns,
            "registry_fingerprint": fingerprint,
            "signature_digest": signature.digest,
            "document_slots": len(self.slots),
            "live_documents": self.live,
            "total_length": self.total_length,
            "vocabulary_size": len(self.vocabulary),
            "unreadable": [
                {"path": path, "mtime_ns": mtime, "size": size}
                for path, (mtime, size) in sorted(self.unreadable.items())
            ],
            "payload_bytes": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "sections": [
                {"name": name, "bytes": len(blob)} for name, blob in sections
            ],
        }
        return (
            json.dumps(header, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
            + payload
        )


def _iso(nanoseconds: int) -> str:
    return (
        datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# The read-only query form
# ---------------------------------------------------------------------------


class StoredIndex:
    """A verified index file, decoded lazily.

    Only the sections a query actually touches are turned into Python objects.
    A one-term query on a fifty-thousand entry vault decodes one posting list
    and the document-length array, not the six megabytes of forward terms that
    exist only so an update can find what to remove.
    """

    def __init__(self, header: dict[str, object], payload: bytes) -> None:
        self.header = header
        self._payload = payload
        self._bounds: dict[str, tuple[int, int]] = {}
        offset = 0
        declared = header.get("sections")
        if not isinstance(declared, list):
            raise IndexUnusable("unreadable", "index header has no section table")
        for entry in declared:
            check_deadline()
            if not isinstance(entry, dict):
                raise IndexUnusable("unreadable", "index section table is malformed")
            name = entry.get("name")
            size = entry.get("bytes")
            if not isinstance(name, str) or not isinstance(size, int) or size < 0:
                raise IndexUnusable("unreadable", "index section table is malformed")
            self._bounds[name] = (offset, offset + size)
            offset += size
        if offset != len(payload):
            raise IndexUnusable("unreadable", "index sections do not fill the payload")
        for name, _ in _SECTIONS:
            check_deadline()
            if name not in self._bounds:
                raise IndexUnusable("unreadable", f"index section {name} is missing")
        self._cache: dict[str, array.array] = {}
        self.document_count = _positive_int(header, "live_documents")
        self.total_length = _positive_int(header, "total_length")
        self.slot_count = _positive_int(header, "document_slots")
        self.vocabulary_size = _positive_int(header, "vocabulary_size")

    # -- section access ---------------------------------------------------

    def _whole(self, name: str, typecode: str) -> array.array:
        cached = self._cache.get(name)
        if cached is None:
            start, stop = self._bounds[name]
            cached = array.array(typecode)
            try:
                cached.frombytes(self._payload[start:stop])
            except ValueError as exc:
                raise IndexUnusable(
                    "unreadable", f"index section {name} has a partial item"
                ) from exc
            self._cache[name] = cached
        return cached

    def _items(
        self, name: str, typecode: str, start_item: int, stop_item: int
    ) -> array.array:
        size = array.array(typecode).itemsize
        base, limit = self._bounds[name]
        start = base + start_item * size
        stop = base + stop_item * size
        if start < base or stop > limit or stop < start:
            raise IndexUnusable("unreadable", f"index section {name} is out of range")
        values = array.array(typecode)
        values.frombytes(self._payload[start:stop])
        return values

    def _blob(self, name: str, start: int, stop: int) -> bytes:
        base, limit = self._bounds[name]
        if start < 0 or base + stop > limit or stop < start:
            raise IndexUnusable("unreadable", f"index section {name} is out of range")
        return self._payload[base + start : base + stop]

    # -- BM25 term source -------------------------------------------------

    def lengths(self) -> Sequence[int]:
        return self._whole("document_lengths", _ORDINAL)

    def term_id(self, term: str) -> int | None:
        """Locate a term by binary search over the lexicographic order.

        The vocabulary itself stays in insertion order so that term ids never
        move when an update introduces a new word; the sorted view is a
        separate array of ids, which an insert shifts with one memmove.
        """

        order = self._whole("vocabulary_sorted", _ORDINAL)
        offsets = self._whole("vocabulary_offsets", _OFFSET)
        encoded = term.encode("utf-8", "surrogatepass")
        low, high = 0, len(order)
        while low < high:
            check_deadline()
            middle = (low + high) // 2
            identifier = order[middle]
            candidate = self._blob(
                "vocabulary", offsets[identifier], offsets[identifier + 1]
            )
            if candidate < encoded:
                low = middle + 1
            elif candidate > encoded:
                high = middle
            else:
                return int(identifier)
        return None

    def postings(self, term: str) -> tuple[Sequence[int], Sequence[int]] | None:
        identifier = self.term_id(term)
        if identifier is None:
            return None
        offsets = self._whole("postings_offsets", _OFFSET)
        start, stop = offsets[identifier], offsets[identifier + 1]
        return (
            self._items("postings_documents", _ORDINAL, start, stop),
            self._items("postings_frequencies", _COUNTER, start, stop),
        )

    def document(self, ordinal: int) -> tuple[str, str]:
        """Return ``(vault-relative path, entry id)`` for one ordinal."""

        offsets = self._whole("string_offsets", _OFFSET)
        raw = self._blob("strings", offsets[ordinal], offsets[ordinal + 1])
        path, _, entry_id = raw.decode("utf-8", "surrogatepass").partition("\n")
        return path, entry_id

    # -- maintenance ------------------------------------------------------

    @property
    def unreadable(self) -> list[str]:
        declared = self.header.get("unreadable")
        if not isinstance(declared, list):
            return []
        return sorted(
            str(item["path"])
            for item in declared
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        )

    def to_mutable(self) -> MutableIndex:
        mutable = MutableIndex()
        vocabulary_offsets = self._whole("vocabulary_offsets", _OFFSET)
        for identifier in range(self.vocabulary_size):
            check_deadline()
            term = self._blob(
                "vocabulary",
                vocabulary_offsets[identifier],
                vocabulary_offsets[identifier + 1],
            ).decode("utf-8", "surrogatepass")
            mutable.vocabulary.append(term)
            mutable.term_ids[term] = identifier
        posting_offsets = self._whole("postings_offsets", _OFFSET)
        for identifier in range(self.vocabulary_size):
            check_deadline()
            start, stop = posting_offsets[identifier], posting_offsets[identifier + 1]
            mutable.postings.append(
                self._items("postings_documents", _ORDINAL, start, stop)
            )
            mutable.frequencies.append(
                self._items("postings_frequencies", _COUNTER, start, stop)
            )

        lengths = self._whole("document_lengths", _ORDINAL)
        mtimes = self._whole("document_mtime_ns", _OFFSET)
        sizes = self._whole("document_size", _OFFSET)
        forward_offsets = self._whole("forward_offsets", _OFFSET)
        for ordinal in range(self.slot_count):
            check_deadline()
            if lengths[ordinal] == FREE_SLOT:
                mutable.slots.append(None)
                mutable.free.append(ordinal)
                continue
            path, entry_id = self.document(ordinal)
            terms = self._items(
                "forward_terms",
                _ORDINAL,
                forward_offsets[ordinal],
                forward_offsets[ordinal + 1],
            )
            mutable.slots.append(
                DocumentSlot(
                    path=path,
                    entry_id=entry_id,
                    mtime_ns=int(mtimes[ordinal]),
                    size=int(sizes[ordinal]),
                    length=int(lengths[ordinal]),
                    terms=list(terms),
                )
            )
            mutable.by_path[path] = ordinal
            mutable.total_length += int(lengths[ordinal])
            mutable.live += 1
        declared = self.header.get("unreadable")
        if isinstance(declared, list):
            for item in declared:
                check_deadline()
                if isinstance(item, dict):
                    mutable.unreadable[str(item["path"])] = (
                        int(item["mtime_ns"]),
                        int(item["size"]),
                    )
        return mutable

    def signatures(self) -> dict[str, tuple[int, int]]:
        lengths = self._whole("document_lengths", _ORDINAL)
        mtimes = self._whole("document_mtime_ns", _OFFSET)
        sizes = self._whole("document_size", _OFFSET)
        found = {
            self.document(ordinal)[0]: (int(mtimes[ordinal]), int(sizes[ordinal]))
            for ordinal in range(self.slot_count)
            if lengths[ordinal] != FREE_SLOT
        }
        declared = self.header.get("unreadable")
        if isinstance(declared, list):
            for item in declared:
                check_deadline()
                if isinstance(item, dict):
                    found[str(item["path"])] = (
                        int(item["mtime_ns"]),
                        int(item["size"]),
                    )
        return found


def _positive_int(header: dict[str, object], key: str) -> int:
    value = header.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IndexUnusable("unreadable", f"index header field {key} is invalid")
    return value


# ---------------------------------------------------------------------------
# File access
# ---------------------------------------------------------------------------


def read_index(layout: NoetrailLayout) -> StoredIndex:
    """Read and verify the stored index, or raise :class:`IndexUnusable`."""

    path = index_path(layout)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise IndexUnusable("absent", "no index has been built") from exc
    except OSError as exc:
        raise IndexUnusable("unreadable", f"index file cannot be read: {exc}") from exc
    newline = raw.find(b"\n")
    if newline < 0:
        raise IndexUnusable("unreadable", "index file has no header line")
    try:
        header = json.loads(raw[:newline].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IndexUnusable("unreadable", "index header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise IndexUnusable("unreadable", "index header is not an object")
    if header.get("format") != INDEX_FORMAT:
        raise IndexUnusable(
            "stale_format", f"index was written by format {header.get('format')!r}"
        )
    if header.get("byte_order") != sys.byteorder or header.get("item_sizes") != {
        _ORDINAL: array.array(_ORDINAL).itemsize,
        _OFFSET: array.array(_OFFSET).itemsize,
    }:
        raise IndexUnusable(
            "stale_format", "index was written for a different machine word layout"
        )
    payload = raw[newline + 1 :]
    if header.get("payload_bytes") != len(payload):
        raise IndexUnusable("unreadable", "index payload is truncated or extended")
    if hashlib.sha256(payload).hexdigest() != header.get("payload_sha256"):
        raise IndexUnusable("unreadable", "index payload checksum does not match")
    return StoredIndex(header, payload)


def write_index(layout: NoetrailLayout, blob: bytes) -> Path:
    """Replace the index file atomically, so a reader sees whole or old.

    Same discipline as ``store.atomic_write_entry``: write a sibling temporary
    file, fsync it, rename over the target. A crash between the two leaves the
    previous index in place, and the freshness check rejects it if the vault
    moved on.
    """

    directory = index_directory(layout)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / INDEX_FILENAME
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=f".{INDEX_FILENAME}.", suffix=".tmp"
        )
    except OSError as exc:
        # The instance config root is read-only in the hardened deployment
        # profile. That is a supported way to run, so it has to fail as a
        # sentence rather than as a traceback.
        raise StorageError(
            f"Search index cannot be written to {directory}: {exc}"
        ) from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as exc:
        raise StorageError(
            f"Search index cannot be written to {directory}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def drop_index(layout: NoetrailLayout) -> bool:
    """Remove the index. Returns whether a file was actually there."""

    path = index_path(layout)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Building and refreshing
# ---------------------------------------------------------------------------


def _document_frequencies(
    path: Path, registry: SchemaRegistry
) -> tuple[str, Counter[str]] | None:
    try:
        metadata, body = parse_frontmatter(path)
    except (OSError, UnicodeError, ValueError):
        return None
    runtime = runtime_metadata(metadata)
    entry_id = runtime.get("id")
    text = searchable_text(runtime, body, registry)
    return (
        entry_id if isinstance(entry_id, str) else "",
        term_frequencies(text.haystack),
    )


@dataclass(frozen=True)
class RefreshReport:
    added: int
    updated: int
    removed: int
    rebuilt: bool
    reason: str
    document_count: int
    vocabulary_size: int
    bytes_written: int
    seconds: float


def refresh(
    root: Path,
    layout: NoetrailLayout,
    registry: SchemaRegistry,
    *,
    force: bool = False,
) -> RefreshReport:
    """Bring the stored index in line with the vault and write it.

    Callers must already hold the exclusive vault lock. Nothing here reads
    ``VaultIndex``: the whole point of an incremental refresh is to touch only
    the files whose ``(mtime_ns, size)`` moved, so pulling in a full scan
    would defeat it.
    """

    started = time.perf_counter()
    built_at_ns = time.time_ns()
    fingerprint = registry_fingerprint(registry)
    signature = scan_signatures(root)

    mutable: MutableIndex | None = None
    reason = "rebuilt"
    if not force:
        try:
            stored = read_index(layout)
        except IndexUnusable as exc:
            reason = exc.state
        else:
            if stored.header.get("data_root") != str(root):
                reason = "stale_data_root"
            elif stored.header.get("registry_fingerprint") != fingerprint:
                reason = "stale_registry"
            else:
                mutable = stored.to_mutable()
                reason = "incremental"

    added = updated = removed = 0
    if mutable is None:
        mutable = MutableIndex()
        previous: dict[str, tuple[int, int]] = {}
    else:
        previous = {
            slot.path: (slot.mtime_ns, slot.size)
            for slot in mutable.slots
            if slot is not None
        }
        # Files that could not be parsed are tracked with a signature too, so
        # a repaired entry is noticed the same way an edited one is.
        previous.update(mutable.unreadable)

    for relative in sorted(set(previous) - set(signature.signatures)):
        check_deadline()
        mutable.remove(relative)
        removed += 1

    for relative, current in sorted(signature.signatures.items()):
        check_deadline()
        if previous.get(relative) == current:
            continue
        existed = relative in previous
        mutable.remove(relative)
        parsed = _document_frequencies(root / relative, registry)
        if parsed is None:
            mutable.mark_unreadable(relative, current[0], current[1])
        else:
            entry_id, frequencies = parsed
            mutable.add(
                relative, entry_id, current[0], current[1], frequencies
            )
        if existed:
            updated += 1
        else:
            added += 1

    blob = mutable.serialize(
        data_root=root,
        fingerprint=fingerprint,
        signature=signature,
        built_at_ns=built_at_ns,
    )
    write_index(layout, blob)
    return RefreshReport(
        added=added,
        updated=updated,
        removed=removed,
        rebuilt=reason != "incremental",
        reason=reason,
        document_count=mutable.live,
        vocabulary_size=len(mutable.vocabulary),
        bytes_written=len(blob),
        seconds=time.perf_counter() - started,
    )


def refresh_if_present(
    root: Path,
    layout: NoetrailLayout,
    registry: SchemaRegistry,
) -> RefreshReport | None:
    """Keep an existing index current after a write; never create one.

    The index is opt-in. A user who has never run ``noetrail index rebuild``
    must not start paying for index maintenance on every capture just because
    the feature shipped, so the absence of the file is the off switch.

    Failures are swallowed on purpose. A cache that could not be updated is a
    cache the freshness check will reject on the next query, which costs a
    scan; letting the exception out would instead fail a capture that has
    already been written to the vault.
    """

    if not index_path(layout).exists():
        return None
    try:
        return refresh(root, layout, registry)
    except (OSError, ValueError, StorageError):
        return None


def open_for_search(
    root: Path,
    layout: NoetrailLayout,
    registry: SchemaRegistry,
) -> tuple[StoredIndex | None, str]:
    """Return the index only if it provably matches the vault right now.

    This is the single gate between "an index file exists" and "an answer may
    come from it". Everything it can go wrong with -- absent, truncated,
    checksum mismatch, written for another vault, written before a schema pack
    changed which fields are searchable, written before an entry was edited --
    ends in the same place: ``None``, and the caller scans.
    """

    try:
        stored = read_index(layout)
    except IndexUnusable as exc:
        return None, exc.state
    if stored.header.get("data_root") != str(root):
        return None, "stale_data_root"
    if stored.header.get("registry_fingerprint") != registry_fingerprint(registry):
        return None, "stale_registry"
    if stored.header.get("signature_digest") != scan_signatures(root).digest:
        return None, "stale_vault"
    return stored, "fresh"


def status(
    root: Path,
    layout: NoetrailLayout,
    registry: SchemaRegistry,
    *,
    verify: bool = False,
) -> dict[str, object]:
    """Describe the index for ``doctor`` and ``index status``.

    ``verify`` re-derives the index from the Markdown and compares the result
    byte for byte. That is as expensive as a rebuild and is the only check
    that catches a file whose content changed while its modification time and
    byte length did not -- the documented blind spot of the cheap check that
    every query runs.
    """

    path = index_path(layout)
    report: dict[str, object] = {"path": str(path), "state": "absent"}
    try:
        stored = read_index(layout)
    except IndexUnusable as exc:
        report["state"] = exc.state
        report["detail"] = exc.detail
        return report

    report.update(
        {
            "built_at": stored.header.get("built_at"),
            "document_count": stored.document_count,
            "vocabulary_size": stored.vocabulary_size,
            "unreadable_entry_count": len(stored.unreadable),
            "bytes": path.stat().st_size,
        }
    )
    if stored.header.get("data_root") != str(root):
        report["state"] = "stale_data_root"
        report["detail"] = "index belongs to a different data root"
        return report
    if stored.header.get("registry_fingerprint") != registry_fingerprint(registry):
        report["state"] = "stale_registry"
        report["detail"] = "searchable field declarations changed since the build"
        return report
    signature = scan_signatures(root)
    if stored.header.get("signature_digest") != signature.digest:
        stored_signatures = stored.signatures()
        report["state"] = "stale_vault"
        report["detail"] = "vault files changed since the build"
        report["changed_entry_count"] = len(
            set(stored_signatures.items()) ^ set(signature.signatures.items())
        )
        return report
    report["state"] = "fresh"
    if verify:
        rebuilt = MutableIndex()
        for relative, current in sorted(signature.signatures.items()):
            check_deadline()
            parsed = _document_frequencies(root / relative, registry)
            if parsed is None:
                rebuilt.mark_unreadable(relative, current[0], current[1])
            else:
                entry_id, frequencies = parsed
                rebuilt.add(relative, entry_id, current[0], current[1], frequencies)
        matches = _content_digest(rebuilt) == _stored_content_digest(stored)
        report["verified"] = matches
        if not matches:
            # Only reachable when a file's bytes changed without its mtime or
            # size changing. The cheap check cannot see that; this one can.
            report["state"] = "drifted"
            report["detail"] = (
                "index content disagrees with the vault although every file "
                "signature matches; rebuild the index"
            )
    return report


def _content_digest(mutable: MutableIndex) -> str:
    """Digest the *meaning* of an index, independent of slot numbering."""

    material = hashlib.sha256()
    for slot in sorted(
        (slot for slot in mutable.slots if slot is not None),
        key=lambda item: item.path,
    ):
        check_deadline()
        terms = sorted(mutable.vocabulary[term_id] for term_id in slot.terms)
        material.update(f"{slot.path}\0{slot.entry_id}\0{slot.length}\0".encode())
        material.update("\0".join(terms).encode("utf-8", "surrogatepass"))
        material.update(b"\n")
    for path in sorted(mutable.unreadable):
        check_deadline()
        material.update(f"!{path}\n".encode())
    return material.hexdigest()


def _stored_content_digest(stored: StoredIndex) -> str:
    return _content_digest(stored.to_mutable())

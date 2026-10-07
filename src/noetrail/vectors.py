#!/usr/bin/env python3
"""The embedding interface: a file contract, and no provider.

Noetrail computes no embeddings, ships no model, and opens no socket for one.
What it defines instead is the shape of a file an *external* provider writes,
and a reranking step that reads it. The reasons are the three properties the
project actually has, in the order they would have been lost:

*Zero dependency.* Every embedding library worth using is a large native
dependency with its own supply chain. Vendoring one ends the claim that a
vault needs the standard library and nothing else, permanently, for a feature
most users will not switch on.

*Packs stay non-executable.* The obvious place to put a provider is a schema
pack, and [ADR 0001](../../docs/adr/0001-core-and-schema-packs.md) makes packs
declarative precisely so that installing one cannot run code. A pack that could
name a callable would end that, and "the pack format is data" is not a property
worth trading for a convenience.

*The vault does not leave the machine.* A hosted embedding provider means the
text of every entry is sent to a third party, and the decision to do that would
be made by a configuration key rather than by the user, once, per query. The
bookmark fetcher is a separate process without vault access for the same
reason. Nothing here dials out: the vectors arrive as a file the caller names
on the command line, and the query vector arrives the same way.

So the provider runs where the user decides, reads entries through the existing
read interface or straight off the Markdown, and writes a sidecar. Noetrail
reads it and reorders BM25's candidates. It never *adds* candidates: an entry
that BM25 did not match cannot be surfaced by a vector, so every result stays
explainable by words that are in the file. That is a real limit on what this
interface can do, and it is the limit that keeps a ranking reproducible by a
reader who has only the vault.

Failure is closed. A sidecar that does not exist, does not parse, or was
written by a different model is a mistake in the caller's own arguments and is
reported. An *entry* whose vector is missing, or whose stored revision no
longer matches the entry on disk, is not: that entry is simply not reranked,
because a vector computed from text that has since changed is a wrong answer
rather than an old one.

Sidecar format -- ``vectors.jsonl``, UTF-8, one JSON object per line::

    {"format": "noetrail-vectors-1", "model": "<label>", "dimensions": 384}
    {"id": "kn_...", "revision": "sha256:...", "vector": [0.01, ...]}
    ...

Query format -- a single JSON object::

    {"format": "noetrail-vectors-1", "model": "<label>", "dimensions": 384,
     "vector": [0.02, ...]}

``model`` is an opaque label the provider chooses. Noetrail does not interpret
it; it only refuses to compare two files that disagree on it, because cosine
similarity between vectors from different models is a confident-looking
number with no meaning.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from noetrail.constants import ID_PATTERN, REVISION_PATTERN
from noetrail.deadline import check_deadline
from noetrail.errors import InvalidRequest

VECTOR_FORMAT = "noetrail-vectors-1"
MAX_DIMENSIONS = 8192
# A sidecar is a local file the caller named, not untrusted input, but an
# accidental pointer at a multi-gigabyte artefact should fail fast rather than
# fill memory.
MAX_SIDECAR_BYTES = 512 * 1024 * 1024

# Reranked entries sort ahead of unranked ones. With complete coverage -- what
# a provider run produces -- the second tier is empty and this is invisible;
# with partial coverage it makes the two groups distinguishable instead of
# interleaving a cosine with a BM25 score, which are not comparable numbers.
RERANKED = 1
NOT_RERANKED = 0

Relevance = tuple[int, float]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidRequest(message)


def _vector(value: object, dimensions: int, context: str) -> list[float]:
    _require(isinstance(value, list), f"{context}: vector must be an array")
    assert isinstance(value, list)
    _require(
        len(value) == dimensions,
        f"{context}: vector must have {dimensions} dimensions",
    )
    numbers: list[float] = []
    for item in value:
        check_deadline()
        _require(
            isinstance(item, (int, float)) and not isinstance(item, bool),
            f"{context}: vector must contain numbers",
        )
        assert isinstance(item, (int, float))
        _require(math.isfinite(item), f"{context}: vector must be finite")
        numbers.append(float(item))
    return numbers


def load_query_vector(path: Path) -> tuple[str, int, list[float]]:
    """Read the query vector the caller computed with their own provider."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidRequest(f"Query vector file cannot be read: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise InvalidRequest("Query vector file is not valid JSON") from exc
    _require(isinstance(document, dict), "Query vector file must be an object")
    assert isinstance(document, dict)
    _require(
        document.get("format") == VECTOR_FORMAT,
        f"Query vector file must declare format {VECTOR_FORMAT}",
    )
    model = document.get("model")
    dimensions = document.get("dimensions")
    _require(isinstance(model, str) and model != "", "Query vector needs a model label")
    _require(
        isinstance(dimensions, int)
        and not isinstance(dimensions, bool)
        and 1 <= dimensions <= MAX_DIMENSIONS,
        f"Query vector dimensions must be between 1 and {MAX_DIMENSIONS}",
    )
    assert isinstance(model, str) and isinstance(dimensions, int)
    return model, dimensions, _vector(document.get("vector"), dimensions, "query")


def load_sidecar(
    path: Path,
    *,
    model: str,
    dimensions: int,
    wanted: dict[str, str],
) -> dict[str, list[float]]:
    """Read vectors for the entries in ``wanted``, mapping id to revision.

    Only the requested entries are kept, and only when the revision the
    provider recorded still matches the entry on disk. Streaming line by line
    rather than loading the document keeps a sidecar for a large vault out of
    memory when a query touched twelve entries.
    """

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InvalidRequest(f"Vector sidecar cannot be read: {exc}") from exc
    _require(
        size <= MAX_SIDECAR_BYTES,
        f"Vector sidecar exceeds {MAX_SIDECAR_BYTES} bytes",
    )

    found: dict[str, list[float]] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            header_line = handle.readline()
            _require(header_line.strip() != "", "Vector sidecar is empty")
            try:
                header = json.loads(header_line)
            except ValueError as exc:
                raise InvalidRequest(
                    "Vector sidecar header is not valid JSON"
                ) from exc
            _require(
                isinstance(header, dict) and header.get("format") == VECTOR_FORMAT,
                f"Vector sidecar must declare format {VECTOR_FORMAT}",
            )
            assert isinstance(header, dict)
            _require(
                header.get("model") == model,
                "Vector sidecar and query vector were written by different "
                "models; a cosine between them is meaningless",
            )
            _require(
                header.get("dimensions") == dimensions,
                "Vector sidecar and query vector have different dimensions",
            )
            for number, line in enumerate(handle, start=2):
                check_deadline()
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise InvalidRequest(
                        f"Vector sidecar line {number} is not valid JSON"
                    ) from exc
                _require(
                    isinstance(record, dict),
                    f"Vector sidecar line {number} must be an object",
                )
                assert isinstance(record, dict)
                entry_id = record.get("id")
                revision = record.get("revision")
                _require(
                    isinstance(entry_id, str) and bool(ID_PATTERN.fullmatch(entry_id)),
                    f"Vector sidecar line {number} has no valid entry id",
                )
                _require(
                    isinstance(revision, str)
                    and bool(REVISION_PATTERN.fullmatch(revision)),
                    f"Vector sidecar line {number} has no valid revision",
                )
                assert isinstance(entry_id, str)
                if wanted.get(entry_id) != revision:
                    # Either not in this result page, or computed from text the
                    # entry no longer holds. Both mean "do not use", and only
                    # the second is a degradation worth nothing further: the
                    # entry keeps its lexical rank.
                    continue
                found[entry_id] = _vector(
                    record.get("vector"), dimensions, f"line {number}"
                )
    except OSError as exc:
        raise InvalidRequest(f"Vector sidecar cannot be read: {exc}") from exc
    return found


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity, with a zero vector treated as no information."""

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def rerank(
    candidates: list[tuple[int, str, str, float]],
    *,
    sidecar: Path,
    query: Path,
) -> dict[int, Relevance]:
    """Reorder BM25 candidates by similarity to a caller-supplied vector.

    ``candidates`` are ``(position, entry id, revision, BM25 score)``. The
    result is keyed by position rather than by identifier, because a vault
    with a duplicated ID is a state `validate` reports and search still has to
    answer without silently dropping one of the two entries.

    Entries with a usable vector sort ahead of entries without one; within
    each group the order is by cosine and by BM25 score respectively.
    """

    model, dimensions, query_vector = load_query_vector(query)
    wanted = {entry_id: revision for _, entry_id, revision, _ in candidates}
    vectors = load_sidecar(
        sidecar, model=model, dimensions=dimensions, wanted=wanted
    )
    return {
        position: (
            (RERANKED, cosine(vectors[entry_id], query_vector))
            if entry_id in vectors
            else (NOT_RERANKED, score)
        )
        for position, entry_id, _, score in candidates
    }

#!/usr/bin/env python3
"""Searchable text, tokenisation, and Okapi BM25 scoring.

This module holds the parts of search that must be identical whether an
answer comes from a persisted index or from a plain vault scan. The persisted
index in :mod:`noetrail.index` is a cache; if the same query can score two
different orders depending on whether that cache exists, the cache has become
a second source of truth. So the field selection, the tokenizer, and the
scoring arithmetic all live here and both paths call them.

Tokenisation is Unicode-aware and deliberately not language-aware: no
stemmer, no stop-word list, no synonym table. A stemmer that is right for
English is wrong for German compounds, and a vault holds whatever its owner
writes. ``str.casefold`` plus NFKC folds case and compatibility forms; nothing
else is removed, so what a reader sees in the Markdown is what the index holds.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING
import unicodedata

from noetrail.constants import WEB_CONTENT_BEGIN, WEB_CONTENT_END
from noetrail.deadline import check_deadline
from noetrail.frontmatter import entry_type_name, metadata_list

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from collections.abc import Sequence

    from noetrail.schema import SchemaRegistry, TypeDefinition

# `\w` minus the underscore: letters, digits, and combining marks in every
# script, without the separator characters a Markdown body is full of. Doing
# this with one compiled pattern rather than a per-character category lookup
# matters -- the tokenizer runs over every byte of every entry on a rebuild.
TOKEN_PATTERN = re.compile(r"[^\W_]+")

# A run longer than this is a base64 blob, a hash, or a minified line, not a
# word anyone searches for. Keeping it out bounds the vocabulary a pathological
# entry can add to the index.
MAX_TOKEN_LENGTH = 64

DEFAULT_K1 = 1.2
DEFAULT_B = 0.75

RANK_SUBSTRING = "substring"
RANK_BM25 = "bm25"
# Both candidate sets, ranked by BM25. Neither mode is a superset of the other:
# BM25 answers a multi-word question that a literal scan cannot, and a literal
# scan answers `hydrat` against "hydration" and `noodle` against "noodles",
# which a tokenised ranking cannot. Running both costs one more pass and
# removes the choice. Only an empty filtered union may use word_form_terms.
RANK_HYBRID = "hybrid"
SEARCH_RANKS = (RANK_SUBSTRING, RANK_BM25, RANK_HYBRID)

# Every BM25 score is strictly positive, because `inverse_document_frequency`
# uses the Lucene form that never goes negative and a posting exists only for a
# term the document contains. A literal match therefore sorts below every
# ranked one at exactly 0.0, without inventing a sentinel.
LITERAL_SCORE = 0.0

# A separate, explicitly labelled last resort, never mixed into lexical hits.
WORD_FORM_SCORE = -1.0
TYPO_SCORE = -2.0
WORD_ENDINGS = ("e", "en", "es", "n", "s")
MIN_FORM_LENGTH = 5
MAX_FORM_QUERY_TERMS = 16


def one_edit_apart(left: str, right: str) -> bool:
    """One insertion, deletion, substitution or adjacent transposition.

    Whole, bounded tokens only: no arbitrary substring similarity, distance
    matrix, language model or learned synonyms. Equal tokens are not typos.
    """
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    position = next(
        (n for n, pair in enumerate(zip(left, right, strict=False))
         if pair[0] != pair[1]),
        len(left),
    )
    if len(left) != len(right):
        return left[position:] == right[position + 1:]
    if left[position + 1:] == right[position + 1:]:
        return True
    return (
        position + 1 < len(left)
        and left[position] == right[position + 1]
        and left[position + 1] == right[position]
        and left[position + 2:] == right[position + 2:]
    )


def typo_terms(query: str, metadata: dict[str, object]) -> tuple[str, ...]:
    """Observed title/alias tokens, at most two, for a tentative fallback.

    Bodies, tags, attributes, numbers, short fragments and hash-like tokens
    cannot create fuzzy candidates. Never turn these temporary query hints
    into stored aliases. Existing lexical and word-form hits take precedence.
    """
    terms = sorted(set(tokenize(query)))
    if not terms or len(terms) > MAX_FORM_QUERY_TERMS:
        return ()
    names = [str(metadata.get("title", "")),
             *(str(alias) for alias in metadata_list(metadata, "aliases"))]

    def eligible(word: str) -> bool:
        return (
            word.isalpha() and len(word) >= MIN_FORM_LENGTH
            and not re.fullmatch(r"[a-f]{8,}", word)
        )

    words = sorted({word for name in names for word in tokenize(name)
                    if eligible(word)})
    found: list[str] = []
    for term in terms:
        check_deadline()
        if not eligible(term):
            continue
        for word in words:
            check_deadline()
            if one_edit_apart(term, word) and word not in found:
                found.append(word)
                if len(found) == 2:
                    return tuple(found)
    return tuple(found)


def word_form_terms(query: str, text: str) -> tuple[str, ...]:
    """Return at most two observed tokens supporting a tentative candidate.

    This is not stemming or semantic equivalence. One spelling must extend
    the other by a listed ending. A compound needs both parts in this entry,
    one as a whole token, and two distinct supporting tokens. Prefix evidence
    alone, arbitrary infixes, numbers and short fragments are insufficient.
    Tokenisation and the persisted BM25 vocabulary remain unchanged.
    """
    terms = sorted(set(tokenize(query)))
    if not terms or len(terms) > MAX_FORM_QUERY_TERMS:
        return ()
    words = {word for word in tokenize(text) if word.isalpha()}
    ordered = sorted(words)

    def prefix_word(prefix: str) -> str | None:
        position = bisect_left(ordered, prefix)
        if position < len(ordered) and ordered[position].startswith(prefix):
            return ordered[position]
        return None

    for term in terms:
        check_deadline()
        if not term.isalpha() or len(term) < MIN_FORM_LENGTH:
            continue
        for ending in WORD_ENDINGS:
            if term + ending in words:
                return (term + ending,)
            if term.endswith(ending):
                stem = term[:-len(ending)]
                if len(stem) >= MIN_FORM_LENGTH and stem in words:
                    return (stem,)
        for split in range(MIN_FORM_LENGTH, len(term) - MIN_FORM_LENGTH + 1):
            check_deadline()
            left, right = term[:split], term[split:]
            if left not in words and right not in words:
                continue
            first, second = prefix_word(left), prefix_word(right)
            if first and second and first != second:
                return (first, second)
    return ()


def tokenize(text: str) -> list[str]:
    """Split text into casefolded, NFKC-normalised search tokens."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [
        token
        for token in TOKEN_PATTERN.findall(normalized)
        if len(token) <= MAX_TOKEN_LENGTH
    ]


def term_frequencies(text: str) -> Counter[str]:
    return Counter(tokenize(text))


@dataclass(frozen=True)
class SearchableText:
    """The text of one entry that search is allowed to look at.

    ``haystack`` is already casefolded because the substring path compares it
    directly; the BM25 path casefolds again inside :func:`tokenize`, which is
    idempotent. ``attributes`` travels with it because the result envelope
    echoes exactly the custom fields that were searched.
    """

    haystack: str
    attributes: dict[str, object]
    custom: TypeDefinition | None


def searchable_text(
    metadata: dict[str, object],
    body: str,
    registry: SchemaRegistry,
) -> SearchableText:
    """Assemble title, declared searchable fields, tags, and body.

    `searchable: true` used to reach only pack-defined types: the field list
    was a hand-written enumeration of built-in names, so `bookmark.language`
    could be declared searchable in the pack and still be findable through no
    interface. Built-in fields are read from the same declaration the custom
    ones already used.
    """

    searched_type = entry_type_name(metadata)
    attributes = metadata.get("attributes", {})
    custom_definition = (
        registry.custom_types.get(searched_type)
        if searched_type is not None
        else None
    )
    builtin_definition = (
        registry.builtin_types.get(searched_type)
        if searched_type is not None
        else None
    )
    searchable_attributes: dict[str, object] = {}
    if custom_definition is not None and isinstance(attributes, dict):
        searchable_attributes = {
            name: attributes[name]
            for name, field in custom_definition.fields.items()
            if field.searchable and name in attributes
        }
    builtin_values: list[object] = []
    if builtin_definition is not None:
        builtin_values = [
            metadata.get(name, "")
            for name, field in sorted(builtin_definition.fields.items())
            if field.searchable
        ]
    searchable_metadata = [
        metadata.get("title", ""),
        " ".join(str(alias) for alias in metadata_list(metadata, "aliases")),
        *(
            " ".join(str(item) for item in value)
            if isinstance(value, list)
            else value
            for value in builtin_values
        ),
        json.dumps(
            searchable_attributes,
            ensure_ascii=False,
            sort_keys=True,
        ),
    ]
    # The provenance fence is machinery, not content. Leaving it in made every
    # fenced bookmark a hit for a query containing "noetrail".
    searchable_body = body
    if WEB_CONTENT_BEGIN in searchable_body:
        searchable_body = searchable_body.replace(WEB_CONTENT_BEGIN, "").replace(
            WEB_CONTENT_END, ""
        )
    haystack = "\n".join(
        [
            *(str(value) for value in searchable_metadata),
            " ".join(str(tag) for tag in metadata_list(metadata, "tags")),
            searchable_body,
        ]
    ).casefold()
    return SearchableText(
        haystack=haystack,
        attributes=searchable_attributes,
        custom=custom_definition,
    )


def registry_fingerprint(registry: SchemaRegistry) -> str:
    """Identify the field selection an index was built with.

    Only what changes the indexed text belongs here: which types exist and
    which of their fields are declared searchable. Installing a pack that adds
    a searchable field has to invalidate the index, because entries already on
    disk now contribute text they did not contribute before -- and nothing
    about those entries' bytes changed, so the file signatures would not
    notice.
    """

    material = json.dumps(
        {
            "core": ["title", "aliases", "tags", "body"],
            "types": {
            type_id: sorted(
                name
                for name, field in definition.fields.items()
                if field.searchable
            )
            for type_id, definition in sorted(registry.types.items())
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def inverse_document_frequency(
    document_count: int, document_frequency: int
) -> float:
    """The Lucene BM25 IDF, which stays positive for very common terms.

    The textbook Robertson-Sparck-Jones form goes negative once a term is in
    more than half the collection, which would let a common word subtract from
    a document's score and push a document that contains *more* of the query
    below one that contains less.
    """

    return math.log(
        1.0
        + (document_count - document_frequency + 0.5)
        / (document_frequency + 0.5)
    )


class TermSource:
    """What BM25 needs from a corpus, whether cached on disk or scanned.

    Both implementations must present ordinals from the same dense range and
    the same document set, otherwise the two paths cannot be compared.
    """

    document_count: int
    total_length: int

    def lengths(self) -> Sequence[int]:  # pragma: no cover - interface
        raise NotImplementedError

    def postings(
        self, term: str
    ) -> tuple[Sequence[int], Sequence[int]] | None:  # pragma: no cover
        raise NotImplementedError


def score_query(
    source: TermSource,
    terms: Sequence[str],
    *,
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> dict[int, float]:
    """Score every document that contains at least one query term.

    Terms are visited in sorted order and each document's score accumulates in
    that same order in both implementations, so the two paths produce not
    merely close but bit-identical floats. Result order therefore cannot
    depend on whether the index existed.
    """

    document_count = source.document_count
    if document_count == 0:
        return {}
    average_length = source.total_length / document_count
    lengths = source.lengths()
    scores: dict[int, float] = {}
    for term in sorted(set(terms)):
        check_deadline()
        posting = source.postings(term)
        if posting is None:
            continue
        ordinals, frequencies = posting
        document_frequency = len(ordinals)
        if document_frequency == 0:
            continue
        weight = inverse_document_frequency(document_count, document_frequency)
        for ordinal, frequency in zip(ordinals, frequencies, strict=True):
            check_deadline()
            length = lengths[ordinal]
            denominator = frequency + k1 * (
                1.0 - b + b * length / average_length
            )
            scores[ordinal] = scores.get(ordinal, 0.0) + weight * (
                frequency * (k1 + 1.0) / denominator
            )
    return scores


class MemoryTermSource(TermSource):
    """A BM25 corpus built in memory from a vault scan.

    This is the fallback path: it is what search uses when no index exists,
    when the index does not match the vault, and when the index file cannot be
    read. It is slower than the cache and always available, which is the
    property that lets the cache be deleted at any time.
    """

    def __init__(self) -> None:
        self._lengths: list[int] = []
        self._postings: dict[str, tuple[list[int], list[int]]] = {}
        self.document_count = 0
        self.total_length = 0

    def add(self, frequencies: Counter[str]) -> int:
        ordinal = self.document_count
        length = sum(frequencies.values())
        self._lengths.append(length)
        self.total_length += length
        self.document_count += 1
        for term, frequency in frequencies.items():
            check_deadline()
            ordinals, counts = self._postings.setdefault(term, ([], []))
            ordinals.append(ordinal)
            counts.append(frequency)
        return ordinal

    def lengths(self) -> Sequence[int]:
        return self._lengths

    def postings(self, term: str) -> tuple[Sequence[int], Sequence[int]] | None:
        return self._postings.get(term)

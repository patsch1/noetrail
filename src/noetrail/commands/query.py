#!/usr/bin/env python3
"""The read-only commands: inventory, search, relations."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
import json
from pathlib import Path
import unicodedata

from noetrail.commands.review import entry_detail
from noetrail.constants import ENTRY_TYPES
from noetrail.deadline import check_deadline
from noetrail.entries import (
    find_entry,
    load_trashed_entries,
    timestamp_sort_key,
)
from noetrail.errors import (
    InvalidRequest,
    NotFound,
)
from noetrail.evidence import match_evidence
from noetrail.frontmatter import (
    entry_type_name,
    metadata_list,
    parse_frontmatter,
    runtime_metadata,
)
from noetrail.index import open_for_search
from noetrail.provenance import search_provenance
from noetrail.relations import load_relation_definitions, load_relation_types
from noetrail.schema import SchemaPackError, TypeDefinition
from noetrail.search import (
    LITERAL_SCORE,
    RANK_HYBRID,
    RANK_SUBSTRING,
    TYPO_SCORE,
    WORD_FORM_SCORE,
    MemoryTermSource,
    SearchableText,
    score_query,
    searchable_text,
    term_frequencies,
    tokenize,
    typo_terms,
    word_form_terms,
)
from noetrail.store import parse_timestamp, revision_for
from noetrail.temporal import (
    relation_holds_at,
    relations_at,
    resolve_as_of,
    temporal_counts,
    temporal_fields,
)
from noetrail.vectors import NOT_RERANKED, Relevance, rerank


def parse_attribute_filters(
    values: list[str],
    type_definition: TypeDefinition,
) -> dict[str, object]:
    filters: dict[str, object] = {}
    for raw in values:
        check_deadline()
        if "=" not in raw:
            raise InvalidRequest(
                "Attribute filters must use field=<JSON value>"
            )
        name, encoded = raw.split("=", 1)
        if name in filters:
            raise InvalidRequest(f"Duplicate attribute filter: {name}")
        field = type_definition.fields.get(name)
        if field is None:
            raise InvalidRequest(
                f"Unknown attribute for {type_definition.qualified_id}: {name}"
            )
        if not field.searchable:
            raise InvalidRequest(
                f"Attribute is not declared searchable: "
                f"{type_definition.qualified_id}.{name}"
            )
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise InvalidRequest(
                f"Attribute filter {name} must contain a JSON value"
            ) from exc
        try:
            field.validate_value(
                value,
                f"attribute filter {type_definition.qualified_id}.{name}",
            )
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc
        filters[name] = value
    return filters


def inventory_counts(
    entries: list[dict[str, object]], field: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for metadata in entries:
        check_deadline()
        value = metadata.get(field)
        if not isinstance(value, str) or not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return {value: counts[value] for value in sorted(counts)}


def command_inventory(args: argparse.Namespace, root: Path) -> int:
    entries = [metadata for _, metadata, _ in args.index.entries]
    unreadable_entry_count = len(args.index.unreadable)
    entry_count = len(entries) + unreadable_entry_count
    as_of_instant, as_of_label = resolve_as_of(args.as_of)
    # `relation_count` counts what the vault asserts *at this instant*, which
    # is why it is not simply the number of stored relation objects any more.
    # The stored total is reported next to it as `recorded_relation_count`.
    relation_counts = temporal_counts(entries, as_of_instant)

    attachment_count = 0
    unresolved_relation_count = 0
    entries_with_attachments = 0
    for metadata in entries:
        check_deadline()
        attachments = metadata.get("attachments", [])
        if isinstance(attachments, list):
            attachment_count += len(attachments)
            if attachments:
                entries_with_attachments += 1
        unresolved_relations = metadata.get("unresolved_relations", [])
        if isinstance(unresolved_relations, list):
            unresolved_relation_count += len(unresolved_relations)

    facets = {
        field: inventory_counts(entries, field)
        for field in (
            "reading_status",
            "bookmark_kind",
            "place_kind",
            "product_kind",
            "experience_kind",
            "interest_status",
        )
    }
    print(
        json.dumps(
            {
                "scope": "active",
                **({"as_of": as_of_label} if as_of_label else {}),
                "complete": unreadable_entry_count == 0,
                "entry_count": entry_count,
                "readable_entry_count": len(entries),
                "unreadable_entry_count": unreadable_entry_count,
                "by_type": inventory_counts(entries, "type"),
                "by_status": inventory_counts(entries, "status"),
                "facets": facets,
                "entries_with_attachments": entries_with_attachments,
                "attachment_count": attachment_count,
                **relation_counts,
                "unresolved_relation_count": unresolved_relation_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


Match = tuple[Path, dict[str, object], str, SearchableText, float]
Keep = Callable[[dict[str, object]], bool]


class _IndexDisagrees(Exception):
    """A file the index lists could not be read after the freshness check.

    Only reachable when something changed the vault without changing any file
    signature -- a revoked ACL is the realistic case. The answer is to throw
    the index away for this query and scan, never to return the entries that
    happened to still be readable.
    """


def _substring_matches(
    args: argparse.Namespace, query: str, keep: Keep
) -> tuple[list[Match], list[str]]:
    matches: list[Match] = []
    for path, metadata, body in args.index.entries:
        check_deadline()
        if not keep(metadata):
            continue
        text = searchable_text(metadata, body, args.registry)
        if query in text.haystack:
            matches.append((path, metadata, body, text, LITERAL_SCORE))
    return matches, args.index.unreadable


def _scanned_bm25_matches(
    args: argparse.Namespace, terms: list[str], keep: Keep
) -> tuple[list[Match], list[str]]:
    """Rank without an index, by building the corpus statistics in memory.

    This is what makes the index optional rather than load-bearing. It costs
    one tokenisation of the whole vault per query, and it is the answer the
    indexed path is required to reproduce exactly.
    """

    entries = args.index.entries
    source = MemoryTermSource()
    texts: list[SearchableText] = []
    for _, metadata, body in entries:
        check_deadline()
        text = searchable_text(metadata, body, args.registry)
        texts.append(text)
        source.add(term_frequencies(text.haystack))
    matches: list[Match] = []
    for ordinal, score in score_query(
        source, terms, k1=args.bm25_k1, b=args.bm25_b
    ).items():
        check_deadline()
        path, metadata, body = entries[ordinal]
        if not keep(metadata):
            continue
        matches.append((path, metadata, body, texts[ordinal], score))
    return matches, args.index.unreadable


def _indexed_matches(
    args: argparse.Namespace,
    root: Path,
    stored: object,
    terms: list[str],
    keep: Keep,
) -> tuple[list[Match], list[str]]:
    """Score from the cache and read only the entries that scored.

    This is the whole saving: a selective query touches the files it is about
    to return metadata for, instead of every file in the vault.
    """

    matches: list[Match] = []
    for ordinal, score in score_query(
        stored, terms, k1=args.bm25_k1, b=args.bm25_b  # type: ignore[arg-type]
    ).items():
        check_deadline()
        relative, _ = stored.document(ordinal)  # type: ignore[attr-defined]
        path = root / relative
        try:
            raw_metadata, body = parse_frontmatter(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise _IndexDisagrees(relative) from exc
        metadata = runtime_metadata(raw_metadata)
        if not keep(metadata):
            continue
        matches.append(
            (
                path,
                metadata,
                body,
                searchable_text(metadata, body, args.registry),
                score,
            )
        )
    return matches, stored.unreadable  # type: ignore[attr-defined]


def _ranked_matches(
    args: argparse.Namespace, root: Path, terms: list[str], keep: Keep
) -> tuple[list[Match], list[str]]:
    """BM25 from the cache when it is usable, otherwise from a scan."""

    stored, _state = open_for_search(root, args.layout, args.registry)
    if stored is not None:
        try:
            return _indexed_matches(args, root, stored, terms, keep)
        except _IndexDisagrees:
            pass
    return _scanned_bm25_matches(args, terms, keep)


def _merged_matches(
    ranked: tuple[list[Match], list[str]],
    literal: tuple[list[Match], list[str]],
) -> tuple[list[Match], list[str]]:
    """Every ranked match, then the literal matches ranking found nothing for.

    Order inside each group does not matter here -- `search_page` sorts by
    score with the identifier as the tie-break -- but membership does. A ranked
    match must win over its literal twin, because it carries the score the
    relevance order is built from.
    """

    ranked_matches, ranked_unreadable = ranked
    literal_matches, literal_unreadable = literal
    seen = {match[0] for match in ranked_matches}
    merged = list(ranked_matches)
    merged.extend(match for match in literal_matches if match[0] not in seen)
    unreadable = list(ranked_unreadable)
    unreadable.extend(
        path for path in literal_unreadable if path not in set(ranked_unreadable)
    )
    return merged, unreadable


def _text_matches(
    args: argparse.Namespace, root: Path, query: str, keep: Keep
) -> tuple[list[Match], list[str]]:
    """Select the entries whose text matches, by whichever route is available.

    The routes have to agree. The substring route is unchanged from before
    BM25 existed; the two BM25 routes differ only in where the term statistics
    come from, and :func:`noetrail.search.score_query` is written so that they
    produce identical floating-point scores rather than merely similar ones.
    Hybrid first unions both lexical predicates. Only an empty filtered
    union may use the separately labelled word-form scan.
    """

    if args.rank == RANK_SUBSTRING:
        return _substring_matches(args, query, keep)
    terms = tokenize(query)
    if not terms:
        # Tokenisation can discard a non-empty query (emoji, punctuation or
        # an oversized token). Only an actually empty query lists everything.
        # Hybrid still has a literal predicate; BM25 has no candidates here.
        if args.rank == RANK_HYBRID or not query:
            return _substring_matches(args, query, keep)
        return [], args.index.unreadable
    ranked = _ranked_matches(args, root, terms, keep)
    if args.rank == RANK_HYBRID:
        merged, unreadable = _merged_matches(
            ranked, _substring_matches(args, query, keep)
        )
        if merged:
            return merged, unreadable
        for path, metadata, body in args.index.entries:
            check_deadline()
            if not keep(metadata):
                continue
            text = searchable_text(metadata, body, args.registry)
            if word_form_terms(query, text.haystack):
                merged.append((path, metadata, body, text, WORD_FORM_SCORE))
        if not merged:
            for path, metadata, body in args.index.entries:
                check_deadline()
                if keep(metadata) and typo_terms(query, metadata):
                    merged.append((path, metadata, body,
                                   searchable_text(metadata, body, args.registry),
                                   TYPO_SCORE))
        return merged, unreadable
    return ranked


def _queries(args: argparse.Namespace) -> list[str]:
    variants = getattr(args, "query_variants", None) or []
    if len(variants) > 3:
        raise InvalidRequest("retrieve accepts at most three query variants")
    queries = [args.query, *variants]
    if variants:
        seen: set[str] = set()
        for query in queries:
            if not query.strip() or "\x00" in query or len(query) > 2_000:
                raise InvalidRequest(
                    "Batch queries must be nonblank, NUL-free "
                    "and at most 2000 characters"
                )
            key = unicodedata.normalize("NFKC", query).casefold().strip()
            if key in seen:
                raise InvalidRequest("Batch queries must be distinct")
            seen.add(key)
    return queries


def _match_kind(score: float) -> str:
    return ("typo" if score == TYPO_SCORE else
            "word_form" if score == WORD_FORM_SCORE else "lexical")


def _multi_text_matches(
    args: argparse.Namespace, root: Path, queries: list[str], keep: Keep,
) -> tuple[list[Match], list[str], dict[Path, list[dict[str, object]]],
           dict[Path, Relevance]]:
    """Fuse complete candidate sets, not separately capped result pages.

    Raw BM25 values from different queries are not comparable. Reciprocal
    rank fusion (k=60) rewards independently matching variants; lexical hits
    precede word-form and typo-only candidates. Identifier ties stay stable.
    """
    merged: dict[Path, Match] = {}
    support: dict[Path, list[dict[str, object]]] = {}
    scores: dict[Path, float] = {}
    unreadable: set[str] = set()
    identities: dict[str, Path] = {}
    for query in queries:
        matches, failed = _text_matches(args, root, query.casefold(), keep)
        unreadable.update(failed)
        matches.sort(key=lambda item: (-item[4], str(item[1].get("id", ""))))
        for position, match in enumerate(matches, 1):
            check_deadline()
            path, metadata, _body, _text, score = match
            entry_id = str(metadata.get("id", ""))
            if entry_id in identities and identities[entry_id] != path:
                raise InvalidRequest(
                    "Duplicate entry ID in batch candidates; run validate"
                )
            identities[entry_id] = path
            if path not in merged or score > merged[path][4]:
                merged[path] = match
            evidence: dict[str, object] = {
                "query": query, "match_kind": _match_kind(score)
            }
            if score == TYPO_SCORE:
                evidence["matched_terms"] = list(typo_terms(query, metadata))
            support.setdefault(path, []).append(evidence)
            scores[path] = scores.get(path, 0.0) + 1.0 / (60 + position)
    relevance = {
        path: (2 if match[4] >= 0 else 1 if match[4] == WORD_FORM_SCORE else 0,
               scores[path])
        for path, match in merged.items()
    }
    return list(merged.values()), sorted(unreadable), support, relevance


def search_page(args: argparse.Namespace, root: Path) -> dict[str, object]:
    if args.limit < 1:
        raise InvalidRequest("--limit must be at least 1")
    if args.limit > 50:
        raise InvalidRequest("--limit must not exceed 50; use --offset to continue")
    if args.offset < 0:
        raise InvalidRequest("--offset must not be negative")
    selected_custom_type = None
    if args.type and args.type not in ENTRY_TYPES:
        try:
            selected_custom_type = args.registry.require_type(args.type)
        except SchemaPackError as exc:
            raise InvalidRequest(str(exc)) from exc
    if args.attribute_filter and selected_custom_type is None:
        raise InvalidRequest(
            "--attribute-filter requires --type with a pack-defined type"
        )
    attribute_filters = (
        parse_attribute_filters(args.attribute_filter, selected_custom_type)
        if selected_custom_type is not None
        else {}
    )
    query = args.query.casefold()
    queries = _queries(args)
    reranking = bool(args.rerank_vectors or args.query_vector)
    if reranking and not (args.rerank_vectors and args.query_vector):
        raise InvalidRequest(
            "--rerank-vectors and --query-vector must be given together"
        )
    if reranking and args.rank == RANK_SUBSTRING:
        # Vectors reorder a ranked candidate set. A literal scan produces no
        # ranking to reorder, so the argument would be silently inert.
        raise InvalidRequest(
            "--rerank-vectors requires --rank bm25 or --rank hybrid"
        )
    if args.bm25_k1 < 0:
        raise InvalidRequest("--bm25-k1 must not be negative")
    if not 0.0 <= args.bm25_b <= 1.0:
        raise InvalidRequest("--bm25-b must lie between 0 and 1")
    read_after = (
        parse_timestamp(args.read_after, "read_after")
        if args.read_after
        else None
    )
    read_before = (
        parse_timestamp(args.read_before, "read_before")
        if args.read_before
        else None
    )
    occurred_after = (
        parse_timestamp(args.occurred_after, "occurred_after")
        if args.occurred_after
        else None
    )
    occurred_before = (
        parse_timestamp(args.occurred_before, "occurred_before")
        if args.occurred_before
        else None
    )
    if read_after and read_before and read_after >= read_before:
        raise InvalidRequest("--read-after must be earlier than --read-before")
    if occurred_after and occurred_before and occurred_after >= occurred_before:
        raise InvalidRequest(
            "--occurred-after must be earlier than --occurred-before"
        )
    if args.relation_predicate:
        predicates = load_relation_types(args.layout)
        if args.relation_predicate not in predicates:
            raise InvalidRequest(
                f"Unknown relation predicate: {args.relation_predicate}"
            )
    # The BM25 index stores term statistics for title, searchable fields, tags
    # and body -- relations are not searched text and are not in it. A
    # point-in-time query therefore changes which *structured* facts hold, not
    # which terms an entry contains, so it needs no index change, cannot make
    # the index stale, and stays a filter applied to whatever the text stage
    # produced. Both indexed and scanned paths read the entry's frontmatter
    # before this filter runs, so both see the same relations.
    as_of_instant, as_of_label = resolve_as_of(args.as_of)

    def keep(
        metadata: dict[str, object],
        *,
        restrict_type: bool = True,
    ) -> bool:
        """Every structured filter, applied before any text is looked at.

        Order matters for cost, not for the answer: these are dictionary
        lookups, while the text test either builds a haystack or -- on the
        BM25 path -- has already been paid for by the index.

        `restrict_type` exists for the empty-result counterfactual below. An
        attribute filter requires `--type`, so lifting the type restriction
        lifts those with it; what remains is the text query and the filters
        that stand on their own.
        """

        if restrict_type and args.type and metadata.get("type") != args.type:
            return False
        attributes = metadata.get("attributes", {})
        if restrict_type and attribute_filters:
            if not isinstance(attributes, dict) or any(
                attributes.get(name) != value
                for name, value in attribute_filters.items()
            ):
                return False
        if args.domain:
            entry_domain = metadata.get("domain") or metadata.get("source_domain")
            if str(entry_domain or "").casefold() != args.domain.casefold():
                return False
        if (
            args.reading_status
            and metadata.get("reading_status") != args.reading_status
        ):
            return False
        if (
            args.bookmark_kind
            and metadata.get("bookmark_kind") != args.bookmark_kind
        ):
            return False
        if args.place_kind and metadata.get("place_kind") != args.place_kind:
            return False
        if args.product_kind and metadata.get("product_kind") != args.product_kind:
            return False
        if (
            args.experience_kind
            and metadata.get("experience_kind") != args.experience_kind
        ):
            return False
        if (
            args.interest_status
            and metadata.get("interest_status") != args.interest_status
        ):
            return False
        if args.experienced is not None:
            has_experience = isinstance(metadata.get("last_experienced_at"), str)
            if has_experience != args.experienced:
                return False
        if args.min_rating is not None:
            rating = metadata.get("rating")
            if isinstance(rating, bool) or not isinstance(rating, int):
                return False
            if rating < args.min_rating:
                return False
        if args.related_id or args.relation_predicate:
            # Only relations in force at the queried instant count as a match.
            # A person who moved on is not returned for their former employer
            # unless the caller names an instant at which that still held.
            relations, _ = relations_at(metadata, as_of_instant)
            has_matching_relation = any(
                isinstance(relation, dict)
                and (
                    args.related_id is None
                    or relation.get("target") == args.related_id
                )
                and (
                    args.relation_predicate is None
                    or relation.get("predicate") == args.relation_predicate
                )
                for relation in relations
            )
            if not has_matching_relation:
                return False
        if occurred_after is not None or occurred_before is not None:
            occurred_at_value = metadata.get("occurred_at")
            if not isinstance(occurred_at_value, str):
                return False
            try:
                occurred_at = datetime.fromisoformat(occurred_at_value)
            except ValueError:
                return False
            if occurred_at.tzinfo is None:
                return False
            if occurred_after is not None and occurred_at < occurred_after:
                return False
            if occurred_before is not None and occurred_at >= occurred_before:
                return False
        if read_after is not None or read_before is not None:
            read_at_value = metadata.get("read_at")
            if not isinstance(read_at_value, str):
                return False
            try:
                read_at = datetime.fromisoformat(read_at_value)
            except ValueError:
                return False
            if read_at.tzinfo is None:
                return False
            if read_after is not None and read_at < read_after:
                return False
            if read_before is not None and read_at >= read_before:
                return False
        return True

    # The path, metadata and body travel next to their result so that the
    # per-entry work whose output only the returned page uses -- the revision
    # hash and the provenance summary -- can wait until the page is sliced.
    query_matches: dict[Path, list[dict[str, object]]] = {}
    fused: dict[Path, Relevance] = {}
    if len(queries) > 1:
        matches, unreadable, query_matches, fused = _multi_text_matches(
            args, root, queries, keep
        )
    else:
        matches, unreadable = _text_matches(args, root, query, keep)
    results: list[tuple[Path, dict[str, object], str, dict[str, object], float]] = []
    for path, metadata, body, text, score in matches:
        check_deadline()
        relations, not_in_force = relations_at(metadata, as_of_instant)
        attachments = metadata.get("attachments", [])
        result: dict[str, object] = {
            "id": metadata.get("id"),
            "type": metadata.get("type"),
            "title": metadata.get("title"),
            "aliases": metadata.get("aliases"),
            "status": metadata.get("status"),
            "updated_at": metadata.get("updated_at"),
            "sensitivity": metadata.get("sensitivity"),
            "url": (
                metadata.get("canonical_url")
                or metadata.get("source_url")
            ),
            "domain": (
                metadata.get("domain")
                or metadata.get("source_domain")
            ),
            "site_name": (
                metadata.get("site_name")
                or metadata.get("source_site_name")
            ),
            "servings": metadata.get("servings"),
            "prep_minutes": metadata.get("prep_minutes"),
            "cook_minutes": metadata.get("cook_minutes"),
            "reading_status": metadata.get("reading_status"),
            "bookmark_kind": metadata.get("bookmark_kind"),
            "read_at": metadata.get("read_at"),
            "place_kind": metadata.get("place_kind"),
            "product_kind": metadata.get("product_kind"),
            "experience_kind": metadata.get("experience_kind"),
            "interest_status": metadata.get("interest_status"),
            "last_experienced_at": metadata.get("last_experienced_at"),
            "occurred_at": metadata.get("occurred_at"),
            "occurred_precision": metadata.get("occurred_precision"),
            "rating": metadata.get("rating"),
            "relation_count": len(relations),
            "attachment_count": (
                len(attachments) if isinstance(attachments, list) else 0
            ),
        }
        result = {
            key: value
            for key, value in result.items()
            if value is not None
        }
        if score == WORD_FORM_SCORE:
            result["match_kind"] = "word_form"
        elif score == TYPO_SCORE:
            result["match_kind"] = "typo"
            result["matched_terms"] = list(typo_terms(args.query, metadata))
        if path in query_matches:
            result["query_matches"] = query_matches[path]
            if score == TYPO_SCORE:
                supported: set[str] = set()
                for evidence in query_matches[path]:
                    observed = evidence.get("matched_terms")
                    if isinstance(observed, list):
                        supported.update(str(term) for term in observed)
                result["matched_terms"] = sorted(supported)
        # Absent for every entry without one, which is nearly all of them: a
        # result page must not grow a key per item to report a zero.
        if not_in_force:
            result["not_in_force_relation_count"] = len(not_in_force)
        if text.custom is not None:
            result["type_version"] = metadata.get("type_version")
            result["attributes"] = text.attributes
        results.append((path, metadata, body, result, score))

    resolved_sort = args.sort
    # A caller who asked for a ranking mode asked for a ranking, so `auto`
    # means the ranking. An empty query has no terms and therefore no ranking
    # to show, which is why it keeps the recency order the literal path uses.
    if (
        resolved_sort == "auto"
        and args.rank != RANK_SUBSTRING
        and any(tokenize(value) for value in queries)
    ):
        resolved_sort = "relevance"
    elif resolved_sort == "auto" and (
        args.type == "experience"
        or args.experience_kind
        or occurred_after
        or occurred_before
    ):
        resolved_sort = "occurred_desc"
    elif resolved_sort == "auto":
        resolved_sort = "updated_desc"
    if resolved_sort == "relevance" and args.rank == RANK_SUBSTRING:
        raise InvalidRequest(
            "--sort relevance requires --rank bm25 or --rank hybrid"
        )
    if reranking and resolved_sort != "relevance":
        # Vectors only ever reorder; asking for a different order and passing
        # them anyway means one of the two arguments is wrong.
        raise InvalidRequest(
            "--rerank-vectors only applies to --sort relevance"
        )

    relevance: dict[int, Relevance] = {
        position: fused.get(item[0], (NOT_RERANKED, item[4]))
        for position, item in enumerate(results)
    }
    if reranking:
        # `revision_for` reads and hashes each candidate. Only the returned
        # page normally pays that; a caller who passed vectors has asked for
        # every candidate to be checked against the text its vector was
        # computed from, and this is what that costs.
        relevance = rerank(
            [
                (
                    position,
                    str(item[3].get("id", "")),
                    revision_for(item[0]),
                    item[4],
                )
                for position, item in enumerate(results)
            ],
            sidecar=Path(args.rerank_vectors),
            query=Path(args.query_vector),
        )
    ranked = [(relevance[position], item) for position, item in enumerate(results)]

    ranked.sort(key=lambda entry: str(entry[1][3].get("id", "")))
    if resolved_sort == "relevance":
        # Descending score, with the identifier order above as the tie-break,
        # so two entries that score the same come back in the same order on
        # every machine and whether or not the index was used.
        ranked.sort(key=lambda entry: (-entry[0][0], -entry[0][1]))
    elif resolved_sort.startswith("updated_"):
        ranked.sort(
            key=lambda entry: timestamp_sort_key(entry[1][3], "updated_at"),
            reverse=resolved_sort.endswith("_desc"),
        )
    elif resolved_sort.startswith("occurred_"):
        ranked.sort(
            key=lambda entry: timestamp_sort_key(entry[1][3], "occurred_at"),
            reverse=resolved_sort.endswith("_desc"),
        )
    else:
        ranked.sort(
            key=lambda entry: str(entry[1][3].get("title", "")).casefold(),
            reverse=resolved_sort.endswith("_desc"),
        )

    total = len(ranked)
    # `revision_for` reads and hashes the whole file. It used to run inside the
    # match loop, so a broad query over 10 000 entries read all 10 000 files a
    # second time to print a page of at most 50. Measured at 10k: 767 ms with
    # the hash in the loop, against 522 ms for the scan alone.
    items: list[dict[str, object]] = []
    for _, (path, metadata, body, result, _) in ranked[
        args.offset : args.offset + args.limit
    ]:
        # Which of the flat values above came off the network, in an object of
        # its own: a client can tell a fetched title from a written one
        # without reading the file or parsing Markdown. Absent for every entry
        # with no web-derived content, which is almost all of them.
        check_deadline()
        provenance = search_provenance(metadata, body, args.body_headings)
        if provenance is not None:
            result["provenance"] = provenance
        if getattr(args, "explain", False):
            fallback_terms = (
                word_form_terms(
                    args.query, searchable_text(metadata, body, args.registry).haystack
                )
                if result.get("match_kind") == "word_form" else
                typo_terms(args.query, metadata)
                if result.get("match_kind") == "typo" else ()
            )
            result["match_evidence"] = match_evidence(
                metadata, body, args.registry, args.query, args.rank,
                args.body_headings,
                matched_terms=fallback_terms,
            )
        items.append({**result, "revision": revision_for(path)})
    next_offset = args.offset + len(items)
    has_more = next_offset < total

    # A type filter that matches nothing says something about the filter, not
    # about the vault. Read on its own, `"total": 0` has been reported to a
    # user as "you have none" while the entries existed under another type --
    # recipes captured as notes before the `recipe` type existed, and every
    # future type will have the same window. So when the type restriction is
    # the thing that emptied the result, the response says what lifting it
    # would find, and under which types.
    #
    # The second scan costs one more pass and runs only on an empty result
    # with a type filter, so an ordinary search pays nothing for it.
    without_type_filter: dict[str, object] | None = None
    if args.type and total == 0:
        def broadened_keep(metadata: dict[str, object]) -> bool:
            return keep(metadata, restrict_type=False)
        if len(queries) > 1:
            broadened, _, _, _ = _multi_text_matches(
                args, root, queries, broadened_keep
            )
        else:
            broadened, _ = _text_matches(args, root, query, broadened_keep)
        if broadened:
            by_type: dict[str, int] = {}
            for _path, metadata, _body, _text, _score in broadened:
                check_deadline()
                name = entry_type_name(metadata) or "unknown"
                by_type[name] = by_type.get(name, 0) + 1
            without_type_filter = {
                "total": len(broadened),
                "by_type": dict(sorted(by_type.items())),
            }

    return {
        "scope": "active",
        "query": args.query,
        **({"queries": queries, "query_fusion": "reciprocal_rank"}
           if len(queries) > 1 else {}),
        **({"as_of": as_of_label} if as_of_label else {}),
        "sort": resolved_sort,
        "total": total,
        **(
            {"without_type_filter": without_type_filter}
            if without_type_filter is not None
            else {}
        ),
        "offset": args.offset,
        "limit": args.limit,
        "returned": len(items),
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        # `inventory` has reported completeness from the start; search did not,
        # so an unreadable entry looked exactly like a miss.
        "complete": not unreadable,
        "unreadable_entry_count": len(unreadable),
        "items": items,
    }


def command_search(args: argparse.Namespace, root: Path) -> int:
    print(json.dumps(search_page(args, root), ensure_ascii=False, indent=2))
    return 0


def command_retrieve(args: argparse.Namespace, root: Path) -> int:
    """Search and return bounded full entry reads in one invocation."""

    if args.limit > 10:
        raise InvalidRequest("--limit must not exceed 10 for retrieve")
    if args.max_body_chars < 1:
        raise InvalidRequest("--max-body-chars must be at least 1")
    if args.max_body_chars > 100_000:
        raise InvalidRequest("--max-body-chars must not exceed 100000")

    page = search_page(args, root)
    compact_items = page.get("items", [])
    if not isinstance(compact_items, list):
        raise RuntimeError("search page returned invalid items")
    remaining = args.max_body_chars
    returned_chars = 0
    details: list[dict[str, object]] = []
    for compact in compact_items:
        check_deadline()
        if not isinstance(compact, dict) or not isinstance(compact.get("id"), str):
            raise RuntimeError("search page returned an invalid item")
        path, metadata, body = find_entry(args.index, compact["id"])
        full = entry_detail(
            root,
            path,
            metadata,
            body,
            args.body_headings,
            args.as_of,
        )
        original_chars = len(body)
        selected_body = body[:remaining]
        body_chars = len(selected_body)
        full["body"] = selected_body
        full["body_chars"] = original_chars
        full["body_returned_chars"] = body_chars
        full["body_truncated"] = body_chars < original_chars
        for field in ("match_kind", "matched_terms", "query_matches"):
            if field in compact:
                full[field] = compact[field]
        details.append(full)
        remaining -= body_chars
        returned_chars += body_chars

    page["mode"] = "retrieve"
    page["max_body_chars"] = args.max_body_chars
    page["body_chars_returned"] = returned_chars
    page["items"] = details
    print(json.dumps(page, ensure_ascii=False, indent=2))
    return 0


def command_relations(args: argparse.Namespace, root: Path) -> int:
    entries = [*args.index.entries, *load_trashed_entries(root)]
    by_id = {
        metadata.get("id"): (path, metadata)
        for path, metadata, _ in entries
        if isinstance(metadata.get("id"), str)
    }
    if args.id not in by_id:
        raise NotFound(f"Entry does not exist: {args.id}")

    as_of_instant, as_of_label = resolve_as_of(args.as_of)
    definitions = load_relation_definitions(args.layout)
    selected_path, selected = by_id[args.id]

    def described(relation: dict[str, object]) -> dict[str, object]:
        target_id = relation.get("target")
        # A relation target is meant to be an entry ID. A list or object there
        # is unhashable, so it cannot be looked up -- report it as dangling
        # rather than aborting the command.
        target = by_id.get(target_id) if isinstance(target_id, str) else None
        described_relation: dict[str, object] = {
            "predicate": relation.get("predicate"),
            "target": target_id,
            "target_title": target[1].get("title") if target else None,
            "target_type": target[1].get("type") if target else None,
            "target_status": target[1].get("status") if target else None,
        }
        return {**described_relation, **temporal_fields(relation)}

    # Split, not filtered: a superseded assertion stays visible here, it just
    # stops sitting among the ones that hold. That is the whole difference
    # between recording a replacement and overwriting a value.
    current, not_in_force = relations_at(selected, as_of_instant)
    outgoing = [described(relation) for relation in current]
    outgoing_not_in_force = [described(relation) for relation in not_in_force]

    incoming: list[dict[str, object]] = []
    incoming_not_in_force: list[dict[str, object]] = []
    for _, metadata, _ in entries:
        check_deadline()
        for relation in metadata_list(metadata, "relations"):
            check_deadline()
            if not isinstance(relation, dict) or relation.get("target") != args.id:
                continue
            predicate = relation.get("predicate")
            definition = definitions.get(str(predicate), {})
            if definition.get("symmetric") is True:
                inverse = predicate
            else:
                inverse = definition.get("inverse", f"inverse_of_{predicate}")
            described_incoming: dict[str, object] = {
                "predicate": inverse,
                "source": metadata.get("id"),
                "source_title": metadata.get("title"),
                "source_type": metadata.get("type"),
                "source_status": metadata.get("status"),
                **temporal_fields(relation),
            }
            if relation_holds_at(relation, as_of_instant):
                incoming.append(described_incoming)
            else:
                incoming_not_in_force.append(described_incoming)

    print(
        json.dumps(
            {
                "id": args.id,
                "type": selected.get("type"),
                "title": selected.get("title"),
                "updated_at": selected.get("updated_at"),
                **({"as_of": as_of_label} if as_of_label else {}),
                "revision": revision_for(selected_path),
                "outgoing": outgoing,
                "incoming": incoming,
                "outgoing_not_in_force": outgoing_not_in_force,
                "incoming_not_in_force": incoming_not_in_force,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0

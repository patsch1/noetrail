"""CLI handlers for declarative saved views."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from noetrail.commands.query import search_page
from noetrail.errors import ConfigurationError
from noetrail.search import DEFAULT_B, DEFAULT_K1, RANK_HYBRID
from noetrail.views import ViewError, ViewRegistry

SEARCH_DEFAULTS: dict[str, object] = {
    "attribute_filter": [],
    "domain": None,
    "reading_status": None,
    "bookmark_kind": None,
    "read_after": None,
    "read_before": None,
    "place_kind": None,
    "product_kind": None,
    "experience_kind": None,
    "interest_status": None,
    "related_id": None,
    "relation_predicate": None,
    "experienced": None,
    "min_rating": None,
    "occurred_after": None,
    "occurred_before": None,
    "as_of": None,
    "offset": 0,
    "sort": "auto",
    "rank": RANK_HYBRID,
    "bm25_k1": DEFAULT_K1,
    "bm25_b": DEFAULT_B,
    "rerank_vectors": None,
    "query_vector": None,
    "type": None,
}


def command_view(args: argparse.Namespace, root: Path) -> int:
    try:
        registry = ViewRegistry.load(args.layout, args.registry)
    except ViewError as exc:
        raise ConfigurationError(f"Invalid saved views: {exc}") from exc
    if args.view_command == "list":
        print(json.dumps(registry.summary(), ensure_ascii=False, indent=2))
        return 0

    try:
        view = registry.require(args.name)
    except ViewError as exc:
        raise ConfigurationError(str(exc)) from exc
    values = {
        **vars(args),
        **SEARCH_DEFAULTS,
        **view.cli_values(),
    }
    if args.limit is not None:
        values["limit"] = args.limit
    if args.offset is not None:
        values["offset"] = args.offset
    page = search_page(argparse.Namespace(**values), root)
    page["view"] = view.name
    page["view_title"] = view.title
    print(json.dumps(page, ensure_ascii=False, indent=2))
    return 0

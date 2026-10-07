#!/usr/bin/env python3
"""Argument parsing and dispatch for the knowledge vault CLI.

This module is the presentation layer and nothing else: it declares the
subcommands, binds each to a handler in `noetrail.commands`, takes the vault
lock the subcommand declared it needs, and turns a `NoetrailError` into a
sentence on stderr plus an exit code. Persistence, parsing, the domain model,
queries, and validation live in the modules it imports, so a library caller
can reach them without going through argparse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from noetrail.body import load_body_headings
from noetrail.commands.capture import command_bookmark, command_capture, command_recipe
from noetrail.commands.curate import command_candidates, command_merge
from noetrail.commands.edit import (
    command_attach,
    command_relate,
    command_retype,
    command_status,
    command_tag,
    command_update,
)
from noetrail.commands.importer import command_import_markdown
from noetrail.commands.maintenance import (
    command_doctor,
    command_index_drop,
    command_index_rebuild,
    command_index_status,
    command_init,
    command_migrate,
    command_schema,
    command_schema_init,
    command_validate,
)
from noetrail.commands.query import (
    command_inventory,
    command_relations,
    command_retrieve,
    command_search,
)
from noetrail.commands.quickstart import command_quickstart
from noetrail.commands.review import (
    command_purge,
    command_restore,
    command_review,
    command_trash,
)
from noetrail.commands.vault_import import (
    command_import_basic_memory,
    command_import_obsidian,
)
from noetrail.commands.views import command_view
from noetrail.constants import (
    ACTIVE_STATUSES,
    BOOKMARK_KINDS,
    EXPERIENCE_KINDS,
    INTEREST_STATUSES,
    MAX_MARKDOWN_IMPORT_FILES,
    PLACE_KINDS,
    PRECISIONS,
    PRODUCT_KINDS,
    READING_STATUSES,
    SEARCH_SORTS,
    SENSITIVITIES,
)
from noetrail.entries import VaultIndex
from noetrail.errors import (
    ConfigurationError,
    InternalError,
    NoetrailError,
)
from noetrail.index import refresh_if_present
from noetrail.layout import (
    LayoutError,
    resolve_layout,
)
from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
)
from noetrail.schema import SchemaPackError, SchemaRegistry
from noetrail.search import (
    DEFAULT_B,
    DEFAULT_K1,
    RANK_HYBRID,
    SEARCH_RANKS,
)
from noetrail.store import vault_lock
from noetrail.version import application_version

# Set by a parent process that parses the CLI's stderr instead of reading it.
# Absent -- which is every interactive and scripted invocation -- the failure
# sentence is printed exactly as `raise SystemExit(message)` used to print it.
ERROR_FORMAT_VARIABLE = "NOETRAIL_ERROR_FORMAT"

AS_OF_HELP = (
    "evaluate relation validity at this ISO 8601 instant instead of now; "
    "a relation without an interval holds at every instant"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noetrail")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {application_version()}",
    )
    parser.add_argument(
        "--root",
        help=(
            "legacy combined root containing .knowledge, vault, and trash; "
            "cannot be combined with separated root options"
        ),
    )
    parser.add_argument(
        "--data-root",
        help="private data root containing vault, trash, and imports",
    )
    parser.add_argument(
        "--config-root",
        help="instance configuration root for local packs and overrides",
    )
    parser.add_argument(
        "--builtins-root",
        help="read-only built-in specifications, schemas, and templates",
    )
    parser.add_argument(
        "--attachment-inbox",
        help="fixed inbound attachment directory; required only by attach",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser(
        "init",
        help="initialize an empty separated data and instance-config layout",
    )
    initialize.set_defaults(handler=command_init, mutating=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="diagnose roots, permissions, packs, and entry schema versions",
    )
    doctor.set_defaults(handler=command_doctor, mutating=False)

    importer = subparsers.add_parser(
        "import",
        help="preview or apply a trusted local import",
    )
    import_commands = importer.add_subparsers(
        dest="import_command",
        required=True,
    )
    markdown_import = import_commands.add_parser(
        "markdown",
        help="import UTF-8 Markdown files from imports/raw as unreviewed notes",
    )
    markdown_import.add_argument(
        "--source",
        required=True,
        help="file or directory below the private data root's imports/raw",
    )
    markdown_import.add_argument(
        "--apply",
        action="store_true",
        help="write the preflighted import plan; default is a dry-run",
    )
    markdown_import.add_argument(
        "--limit",
        type=int,
        help=f"process only the first N files (maximum {MAX_MARKDOWN_IMPORT_FILES})",
    )
    markdown_import.add_argument(
        "--sensitivity",
        choices=sorted(SENSITIVITIES),
        default="personal",
    )
    markdown_import.add_argument("--tag", action="append", default=[])
    markdown_import.set_defaults(
        handler=command_import_markdown,
        mutating=lambda args: bool(args.apply),
    )

    for name, handler, description in (
        (
            "obsidian",
            command_import_obsidian,
            "import an Obsidian vault: wikilinks, properties, inline tags, "
            "and embedded images",
        ),
        (
            "basic-memory",
            command_import_basic_memory,
            "import a Basic Memory project: frontmatter, observations, and "
            "typed relation lines",
        ),
    ):
        vault_import = import_commands.add_parser(name, help=description)
        vault_import.add_argument(
            "--source",
            required=True,
            help="vault directory below the private data root's imports/raw",
        )
        vault_import.add_argument(
            "--apply",
            action="store_true",
            help="write the preflighted import plan; default is a dry-run",
        )
        vault_import.add_argument(
            "--limit",
            type=int,
            help=(
                f"process only the first N notes (maximum "
                f"{MAX_MARKDOWN_IMPORT_FILES})"
            ),
        )
        vault_import.add_argument(
            "--strict",
            action="store_true",
            help=(
                "fail the run instead of skipping notes that cannot be read "
                "or whose frontmatter is unsupported"
            ),
        )
        vault_import.add_argument(
            "--sensitivity",
            choices=sorted(SENSITIVITIES),
            default="personal",
        )
        vault_import.add_argument("--tag", action="append", default=[])
        vault_import.set_defaults(
            handler=handler,
            mutating=lambda args: bool(args.apply),
        )

    quickstart = subparsers.add_parser(
        "quickstart",
        help="create an instance, add sample entries, print the MCP config",
    )
    quickstart.add_argument(
        "--path",
        help="directory to hold data/ and config/ (default: ~/noetrail)",
    )
    quickstart.add_argument(
        "--json",
        action="store_true",
        help="machine-readable report instead of the printed walkthrough",
    )
    quickstart.set_defaults(handler=command_quickstart, mutating=True)

    capture = subparsers.add_parser("capture", help="create one vault entry")
    capture.add_argument(
        "--type",
        default="thought",
        help="built-in type or installed qualified pack/type ID",
    )
    capture.add_argument("--title")
    capture.add_argument("--alias", action="append", default=[])
    capture.add_argument("--text", help="body text; reads standard input when omitted")
    capture.add_argument(
        "--attributes-file",
        help="JSON object of attributes for a pack-defined type",
    )
    capture.add_argument(
        "--sensitivity", choices=sorted(SENSITIVITIES), default="personal"
    )
    capture.add_argument("--tag", action="append", default=[])
    capture.add_argument(
        "--relation",
        action="append",
        default=[],
        help="typed relation as predicate:target-id",
    )
    capture.add_argument(
        "--unresolved-relation",
        action="append",
        default=[],
        help="pending relation as predicate:free-text-reference",
    )
    capture.add_argument("--occurred-at")
    capture.add_argument("--occurred-precision", choices=sorted(PRECISIONS))
    capture.add_argument("--place-kind", choices=sorted(PLACE_KINDS))
    capture.add_argument("--product-kind", choices=sorted(PRODUCT_KINDS))
    capture.add_argument("--interest-status", choices=sorted(INTEREST_STATUSES))
    capture.add_argument("--servings")
    capture.add_argument("--prep-minutes", type=int)
    capture.add_argument("--cook-minutes", type=int)
    capture.add_argument("--experience-kind", choices=sorted(EXPERIENCE_KINDS))
    capture.add_argument("--rating", type=int, choices=range(1, 6))
    capture.add_argument("--allow-duplicate", action="store_true")
    capture.set_defaults(handler=command_capture, mutating=True)

    recipe = subparsers.add_parser(
        "recipe",
        help="create a recipe from one public source URL and optional user text",
    )
    recipe.add_argument("url")
    recipe.add_argument("--canonical-url")
    recipe.add_argument("--title")
    recipe.add_argument("--alias", action="append", default=[])
    recipe.add_argument("--text", help="optional user-supplied recipe or note")
    recipe.add_argument("--site-name")
    recipe.add_argument("--source-description")
    recipe.add_argument(
        "--sensitivity", choices=sorted(SENSITIVITIES), default="personal"
    )
    recipe.add_argument("--tag", action="append", default=[])
    recipe.add_argument(
        "--relation",
        action="append",
        default=[],
        help="typed relation as predicate:target-id",
    )
    recipe.add_argument(
        "--unresolved-relation",
        action="append",
        default=[],
        help="pending relation as predicate:free-text-reference",
    )
    recipe.add_argument(
        "--interest-status",
        choices=sorted(INTEREST_STATUSES),
        default="none",
    )
    recipe.add_argument("--servings")
    recipe.add_argument("--prep-minutes", type=int)
    recipe.add_argument("--cook-minutes", type=int)
    recipe.set_defaults(handler=command_recipe, mutating=True)

    bookmark = subparsers.add_parser(
        "bookmark", help="create an enriched bookmark from agent-provided metadata"
    )
    bookmark.add_argument("url", nargs="?")
    bookmark.add_argument(
        "--metadata-file",
        help="JSON object with fetched metadata, summary, tags, and optional note",
    )
    bookmark.add_argument("--allow-duplicate", action="store_true")
    bookmark.set_defaults(handler=command_bookmark, mutating=True)

    update = subparsers.add_parser(
        "update",
        help="update content or metadata, including a personal experience",
    )
    update.add_argument("id")
    update.add_argument("--patch-file", required=True)
    update.add_argument(
        "--expected-revision",
        help="reject the update if the entry changed after it was read",
    )
    update.add_argument(
        "--allow-replace-body",
        action="store_true",
        help="allow full body replacement after explicit user approval",
    )
    update.set_defaults(handler=command_update, mutating=True)

    attach = subparsers.add_parser(
        "attach",
        help="copy one supported image from the configured inbox into the vault",
    )
    attach.add_argument("id")
    attach.add_argument("source", help="image path below the configured inbox")
    attach.add_argument(
        "--expected-revision",
        required=True,
        help="reject the attachment if the entry changed after it was read",
    )
    attach.add_argument(
        "--expected-source-sha256",
        help="reject the attachment if its bytes changed after inbox listing",
    )
    attach.add_argument("--caption")
    attach.add_argument(
        "--experienced-at",
        help="associate the image with a recorded place/product experience",
    )
    attach.set_defaults(handler=command_attach, mutating=True)

    relate = subparsers.add_parser(
        "relate", help="add, resolve, or remove a relation"
    )
    relate.add_argument("id", help="source entry ID")
    relate.add_argument("predicate")
    relate.add_argument(
        "target", help="target entry ID, or free-text reference with --unresolved"
    )
    relate.add_argument("--remove", action="store_true")
    relate.add_argument(
        "--unresolved",
        action="store_true",
        help="store or remove a pending free-text relation",
    )
    relate.add_argument(
        "--resolve-reference",
        help="remove a matching pending reference while adding this relation",
    )
    relate.add_argument(
        "--valid-from",
        help=(
            "ISO 8601 instant from which the relation holds in the world; "
            "without it the relation carries no interval and holds always"
        ),
    )
    relate.add_argument(
        "--valid-until",
        help="ISO 8601 instant at which the relation stops holding",
    )
    relate.add_argument(
        "--supersedes",
        help=(
            "target ID of the relation with the same predicate that this one "
            "replaces; the replaced relation stays readable and is closed at "
            "--valid-from"
        ),
    )
    relate.add_argument(
        "--expected-revision",
        help="reject the update if the entry changed after it was read",
    )
    relate.set_defaults(handler=command_relate, mutating=True)

    tag = subparsers.add_parser("tag", help="add or remove tags")
    tag.add_argument("id")
    tag.add_argument("tags", nargs="+")
    tag.add_argument("--remove", action="store_true")
    tag.add_argument(
        "--expected-revision",
        help="reject the update if the entry changed after it was read",
    )
    tag.set_defaults(handler=command_tag, mutating=True)

    retype = subparsers.add_parser(
        "retype",
        help="correct the type of an entry that was filed under the wrong one",
    )
    retype.add_argument("id")
    retype.add_argument(
        "--to",
        required=True,
        help="target type; built-in or pack-defined",
    )
    retype.add_argument(
        "--apply",
        action="store_true",
        help="write the previewed change; default is a dry-run",
    )
    retype.add_argument(
        "--drop-unsupported",
        action="store_true",
        help="discard attributes the target type does not define",
    )
    retype.add_argument(
        "--expected-revision",
        help="reject the change if the entry changed after it was read",
    )
    retype.set_defaults(handler=command_retype, mutating=True)

    status = subparsers.add_parser(
        "status", help="change lifecycle, reading, or interest status"
    )
    status.add_argument("id")
    status.add_argument("--lifecycle", choices=sorted(ACTIVE_STATUSES))
    status.add_argument("--reading", choices=sorted(READING_STATUSES))
    status.add_argument("--interest", choices=sorted(INTEREST_STATUSES))
    status.add_argument(
        "--expected-revision",
        help="reject the update if the entry changed after it was read",
    )
    status.set_defaults(handler=command_status, mutating=True)

    review = subparsers.add_parser(
        "review", help="list or complete knowledge review items"
    )
    review.add_argument("id", nargs="?")
    review.add_argument(
        "--complete",
        action="store_true",
        help="mark one resolved entry as reviewed",
    )
    review.add_argument(
        "--limit",
        type=int,
        default=50,
        help="maximum queue items to display",
    )
    review.add_argument(
        "--as-of",
        help=f"with an entry ID, {AS_OF_HELP}",
    )
    review.add_argument(
        "--expected-revision",
        help="with --complete, reject a stale review decision",
    )
    review.set_defaults(
        handler=command_review,
        mutating=lambda args: bool(args.complete),
    )

    trash = subparsers.add_parser(
        "trash", help="move one entry to trash, or list trashed entries"
    )
    trash.add_argument("target", help="entry ID, or 'list'")
    trash.add_argument("--reason")
    trash.add_argument("--retention-days", type=int, default=90)
    trash.add_argument(
        "--expected-revision",
        help="reject deletion if the entry changed after it was read",
    )
    trash.set_defaults(
        handler=command_trash,
        mutating=lambda args: args.target != "list",
    )

    restore = subparsers.add_parser(
        "restore", help="restore one trashed entry to its original path"
    )
    restore.add_argument("id")
    restore.add_argument(
        "--expected-revision",
        help="reject restoration if the trash entry changed after it was read",
    )
    restore.set_defaults(handler=command_restore, mutating=True)

    purge = subparsers.add_parser(
        "purge", help="preview or permanently remove entries from trash"
    )
    purge_selection = purge.add_mutually_exclusive_group()
    purge_selection.add_argument("--id", help="select one exact trashed entry ID")
    purge_selection.add_argument(
        "--older-than-days",
        type=int,
        help="select entries deleted at least this many days ago",
    )
    purge_selection.add_argument(
        "--expired",
        action="store_true",
        help="select entries whose purge_after time has passed (default)",
    )
    purge.add_argument(
        "--apply",
        action="store_true",
        help="permanently remove selected local trash files",
    )
    purge.set_defaults(
        handler=command_purge,
        mutating=lambda args: bool(args.apply),
    )

    migrate = subparsers.add_parser(
        "migrate", help="upgrade entries to the current schema version"
    )
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="write the planned migrations (default: preview only)",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    migrate.add_argument(
        "--backup-dir",
        help=(
            "copy every entry about to be rewritten into this empty directory "
            "first"
        ),
    )
    migrate.set_defaults(
        handler=command_migrate,
        mutating=lambda args: args.apply,
    )

    inventory = subparsers.add_parser(
        "inventory",
        help="summarize all active entries without titles or bodies",
    )
    inventory.add_argument(
        "--as-of",
        help=AS_OF_HELP,
    )
    inventory.set_defaults(handler=command_inventory, mutating=False)

    search = subparsers.add_parser("search", help="search titles, tags, and bodies")
    search.add_argument("query")
    search.add_argument(
        "--explain", action="store_true",
        help="include bounded matching excerpts and their recorded origin",
    )
    search.add_argument(
        "--type",
        help="built-in type or installed qualified pack/type ID",
    )
    search.add_argument(
        "--attribute-filter",
        action="append",
        default=[],
        help=(
            "exact searchable custom-field filter as field=<JSON value>; "
            "repeatable and requires a qualified --type"
        ),
    )
    search.add_argument("--domain")
    search.add_argument("--reading-status", choices=sorted(READING_STATUSES))
    search.add_argument("--bookmark-kind", choices=sorted(BOOKMARK_KINDS))
    search.add_argument("--place-kind", choices=sorted(PLACE_KINDS))
    search.add_argument("--product-kind", choices=sorted(PRODUCT_KINDS))
    search.add_argument("--experience-kind", choices=sorted(EXPERIENCE_KINDS))
    search.add_argument("--interest-status", choices=sorted(INTEREST_STATUSES))
    search.add_argument(
        "--related-id",
        help="include entries with a relation to this stable entry ID",
    )
    search.add_argument(
        "--relation-predicate",
        help="restrict relation matching to this configured predicate",
    )
    experience_filter = search.add_mutually_exclusive_group()
    experience_filter.add_argument(
        "--experienced", dest="experienced", action="store_true"
    )
    experience_filter.add_argument(
        "--not-experienced", dest="experienced", action="store_false"
    )
    search.set_defaults(experienced=None)
    search.add_argument("--min-rating", type=int, choices=range(1, 6))
    search.add_argument(
        "--read-after",
        help="include bookmarks read at or after this ISO 8601 timestamp",
    )
    search.add_argument(
        "--read-before",
        help="include bookmarks read before this ISO 8601 timestamp",
    )
    search.add_argument(
        "--occurred-after",
        help="include experiences at or after this ISO 8601 timestamp",
    )
    search.add_argument(
        "--occurred-before",
        help="include experiences before this ISO 8601 timestamp",
    )
    search.add_argument(
        "--limit",
        type=int,
        default=20,
        help="matching entries in this page (maximum 50)",
    )
    search.add_argument(
        "--offset",
        type=int,
        default=0,
        help="zero-based result offset for the next page",
    )
    search.add_argument(
        "--sort",
        choices=sorted(SEARCH_SORTS),
        default="auto",
        help=(
            "stable result order; auto uses newest occurrence for experience "
            "queries, relevance under a ranking mode, and newest update "
            "otherwise"
        ),
    )
    search.add_argument(
        "--rank",
        choices=sorted(SEARCH_RANKS),
        default=RANK_HYBRID,
        help=(
            "hybrid (default) returns both and ranks by BM25, with labelled "
            "word-form candidates only when both are empty; substring "
            "matches the query literally against the searched text; bm25 "
            "tokenizes it and ranks entries containing any term"
        ),
    )
    search.add_argument(
        "--bm25-k1",
        type=float,
        default=DEFAULT_K1,
        help="BM25 term-frequency saturation (default: %(default)s)",
    )
    search.add_argument(
        "--bm25-b",
        type=float,
        default=DEFAULT_B,
        help="BM25 length normalization between 0 and 1 (default: %(default)s)",
    )
    search.add_argument(
        "--rerank-vectors",
        help=(
            "reorder BM25 results by a vector sidecar produced outside "
            "Noetrail; see docs/embeddings.md. Noetrail computes no "
            "embeddings and makes no network call"
        ),
    )
    search.add_argument(
        "--query-vector",
        help="JSON file holding the query vector, from the same provider",
    )
    search.add_argument(
        "--as-of",
        help=AS_OF_HELP,
    )
    search.set_defaults(handler=command_search, mutating=False)

    retrieve = subparsers.add_parser(
        "retrieve",
        help="search and return bounded full entries in one operation",
    )
    retrieve.add_argument("query")
    retrieve.add_argument(
        "--type",
        help="built-in type or installed qualified pack/type ID",
    )
    retrieve.add_argument(
        "--limit",
        type=int,
        default=5,
        help="full entries to return (maximum 10)",
    )
    retrieve.add_argument(
        "--max-body-chars",
        type=int,
        default=30_000,
        help="shared character budget for all returned bodies (maximum 100000)",
    )
    retrieve.add_argument(
        "--rank",
        choices=sorted(SEARCH_RANKS),
        default=RANK_HYBRID,
    )
    retrieve.add_argument("--as-of", help=AS_OF_HELP)
    retrieve.set_defaults(
        handler=command_retrieve,
        mutating=False,
        attribute_filter=[],
        domain=None,
        reading_status=None,
        bookmark_kind=None,
        read_after=None,
        read_before=None,
        place_kind=None,
        product_kind=None,
        experience_kind=None,
        interest_status=None,
        related_id=None,
        relation_predicate=None,
        experienced=None,
        min_rating=None,
        occurred_after=None,
        occurred_before=None,
        offset=0,
        sort="auto",
        bm25_k1=DEFAULT_K1,
        bm25_b=DEFAULT_B,
        rerank_vectors=None,
        query_vector=None,
    )

    view = subparsers.add_parser(
        "view",
        help="list or run declarative saved search views",
    )
    view_commands = view.add_subparsers(dest="view_command", required=True)
    view_list = view_commands.add_parser("list", help="list configured views")
    view_list.set_defaults(handler=command_view, mutating=False)
    view_run = view_commands.add_parser("run", help="run one configured view")
    view_run.add_argument("name")
    view_run.add_argument("--limit", type=int)
    view_run.add_argument("--offset", type=int)
    view_run.set_defaults(handler=command_view, mutating=False)

    index = subparsers.add_parser(
        "index",
        help="build, inspect, or discard the derived BM25 search index",
    )
    index_commands = index.add_subparsers(dest="index_command", required=True)
    index_status = index_commands.add_parser(
        "status",
        help="report whether the index exists and matches the vault",
    )
    index_status.add_argument(
        "--verify",
        action="store_true",
        help=(
            "re-derive the index from the Markdown and compare content; as "
            "expensive as a rebuild"
        ),
    )
    index_status.set_defaults(handler=command_index_status, mutating=False)
    index_rebuild = index_commands.add_parser(
        "rebuild",
        help="create or refresh the index from the current vault",
    )
    index_rebuild.add_argument(
        "--full",
        action="store_true",
        help="discard the existing index instead of updating it incrementally",
    )
    index_rebuild.set_defaults(handler=command_index_rebuild, mutating=True)
    index_drop = index_commands.add_parser(
        "drop",
        help="delete the index; search falls back to scanning the vault",
    )
    index_drop.set_defaults(handler=command_index_drop, mutating=True)

    relations = subparsers.add_parser(
        "relations", help="show forward and derived inverse relations"
    )
    relations.add_argument("id")
    relations.add_argument(
        "--as-of",
        help=AS_OF_HELP,
    )
    relations.set_defaults(handler=command_relations, mutating=False)

    validate = subparsers.add_parser("validate", help="validate all vault entries")
    validate.set_defaults(handler=command_validate, mutating=False)

    schema = subparsers.add_parser(
        "schema",
        help="inspect and validate declarative schema packs",
    )
    schema_commands = schema.add_subparsers(
        dest="schema_command",
        required=True,
    )
    schema_list = schema_commands.add_parser(
        "list",
        help="list loaded packs and qualified type IDs",
    )
    schema_list.set_defaults(handler=command_schema, mutating=False)
    schema_validate = schema_commands.add_parser(
        "validate",
        help="validate all built-in and local pack definitions",
    )
    schema_validate.set_defaults(handler=command_schema, mutating=False)
    schema_explain = schema_commands.add_parser(
        "explain",
        help="show one resolved type and its generated JSON Schema",
    )
    schema_explain.add_argument("type_id")
    schema_explain.set_defaults(handler=command_schema, mutating=False)
    schema_validate_pack = schema_commands.add_parser(
        "validate-pack",
        help="validate one uninstalled pack directory",
    )
    schema_validate_pack.add_argument("path")
    schema_validate_pack.set_defaults(handler=command_schema, mutating=False)
    schema_init = schema_commands.add_parser(
        "init",
        help="create a minimal data-only schema pack",
    )
    schema_init.add_argument("pack_id")
    schema_init.add_argument("--type", dest="type_id", default="item")
    schema_init.add_argument(
        "--output",
        help="existing parent directory; defaults to the instance packs root",
    )
    schema_init.set_defaults(handler=command_schema_init, mutating=True)
    candidates = subparsers.add_parser(
        "candidates", help="suggest possible duplicates or related titles for one entry"
    )
    candidates.add_argument("id")
    candidates.add_argument("--limit", type=int, default=20)
    candidates.add_argument("--offset", type=int, default=0)
    candidates.set_defaults(handler=command_candidates, mutating=False)
    merge = subparsers.add_parser("merge", help="preview a conservative entry merge")
    merge.add_argument("source_id")
    merge.add_argument("target_id")
    merge.add_argument("--apply", action="store_true")
    merge.add_argument("--expected-plan")
    merge.set_defaults(handler=command_merge, mutating=lambda args: args.apply)
    return parser


def command_is_mutating(args: argparse.Namespace) -> bool:
    """Report whether the selected subcommand needs the exclusive vault lock.

    Every subparser declares this through ``set_defaults`` so a new writing
    command cannot silently run under a shared lock.
    """

    declared = getattr(args, "mutating", None)
    if declared is None:
        raise InternalError(
            f"Command {args.command!r} does not declare whether it mutates "
            "the vault; this is a Noetrail defect"
        )
    if callable(declared):
        return bool(declared(args))
    return bool(declared)


def run_command(argv: list[str] | None = None) -> int:
    """Run one CLI invocation and return its exit code.

    Separated from ``main`` so that a long-lived process, such as the Knowledge
    MCP server, can execute a command without paying interpreter startup and
    module import again.
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    # Both create the instance they operate on, so neither can resolve a
    # layout or take the vault lock before its handler has run. `quickstart`
    # takes the exclusive lock itself, around its own writes.
    if args.command in {"init", "quickstart"}:
        return args.handler(args)
    try:
        layout = resolve_layout(
            root=args.root,
            data_root=args.data_root,
            config_root=args.config_root,
            builtins_root=args.builtins_root,
        )
    except LayoutError as exc:
        if args.command == "doctor":
            print(
                json.dumps(
                    {
                        "ok": False,
                        "current_schema_version": CURRENT_SCHEMA_VERSION,
                        "entry_schema_versions": {},
                        "checks": [
                            {
                                "name": "layout",
                                "status": "error",
                                "detail": str(exc),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        raise ConfigurationError(str(exc)) from exc
    args.layout = layout
    try:
        args.registry = SchemaRegistry.load(layout)
    except SchemaPackError as exc:
        if args.command == "doctor":
            print(
                json.dumps(
                    {
                        "ok": False,
                        "current_schema_version": CURRENT_SCHEMA_VERSION,
                        "entry_schema_versions": {},
                        "checks": [
                            {
                                "name": "schema_registry",
                                "status": "error",
                                "detail": f"invalid schema pack: {exc}",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        raise ConfigurationError(f"Invalid schema pack: {exc}") from exc
    args.body_headings = load_body_headings(layout)
    root = layout.data_root
    mutating = command_is_mutating(args)
    with vault_lock(root, exclusive=mutating):
        # Constructed inside the lock and scanned on first use, so a command
        # that never looks at the vault still pays nothing and one that looks
        # five times pays once.
        args.index = VaultIndex(root)
        status = args.handler(args, root)
        if mutating and status == 0 and args.command != "index":
            # The derived search index is maintained here rather than inside
            # each handler for two reasons. It is the one place that already
            # holds the exclusive lock for every writing command, so a new
            # command cannot forget it; and `tests/test_locking.py` forbids a
            # handler from touching the shared `VaultIndex` snapshot after a
            # write, which is a different object with the same hazard. This
            # runs after the handler has finished writing and reads the files,
            # not the snapshot. It does nothing when no index exists.
            refresh_if_present(root, layout, args.registry)
        return status


def main() -> int:
    """Run one invocation and translate a refusal into CLI behaviour.

    This is the single place where the typed failures of ``noetrail.errors``
    become what a shell sees: the sentence on stderr and a non-zero status,
    exactly what ``raise SystemExit(message)`` produced before. Callers that
    are not a terminal -- the MCP server, a library user -- use ``run_command``
    and catch ``NoetrailError`` with its ``code`` intact.
    """

    try:
        return run_command()
    except NoetrailError as exc:
        if os.environ.get(ERROR_FORMAT_VARIABLE) == "json":
            # Opt-in, and set only by a caller that spawns the CLI itself --
            # the MCP server, which would otherwise have to recover the code
            # by matching the sentence it just printed.
            print(
                json.dumps(exc.as_dict(), ensure_ascii=False),
                file=sys.stderr,
            )
        else:
            print(exc.message, file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

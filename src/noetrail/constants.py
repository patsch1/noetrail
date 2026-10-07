#!/usr/bin/env python3
"""Vault-wide literals: entry types, enumerations, and limits."""

from __future__ import annotations

import re

from noetrail.migrations import (
    CURRENT_SCHEMA_VERSION,
)

ENTRY_TYPES = {
    "thought",
    "memory",
    "note",
    "person",
    "project",
    "media",
    "source",
    "place",
    "product",
    "recipe",
    "experience",
    "bookmark",
}


CAPTURE_TYPES = ENTRY_TYPES - {"bookmark"}


ACTIVE_STATUSES = {"active", "unreviewed", "archived"}


STATUSES = ACTIVE_STATUSES | {"trashed"}


SENSITIVITIES = {"normal", "personal", "sensitive"}


PRECISIONS = {"unknown", "year", "month", "day", "datetime", "approximate"}


READING_STATUSES = {"unread", "reading", "read", "reference"}


BOOKMARK_KINDS = {
    "article",
    "video",
    "podcast",
    "tool",
    "product",
    "website",
    "other",
    "unknown",
}


PLACE_KINDS = {"bar", "restaurant", "cafe", "shop", "other"}


PRODUCT_KINDS = {"rum", "spirit", "wine", "beer", "drink", "food", "other"}


INTEREST_STATUSES = {"none", "wishlist", "planned"}


INTEREST_TYPES = {"place", "product", "recipe"}


LEGACY_EXPERIENCE_TYPES = {"place", "product"}


EXPERIENCE_KINDS = {
    "tasting",
    "visit",
    "dining",
    "watching",
    "listening",
    "reading",
    "cooking",
    "other",
}


FETCH_STATUSES = {"complete", "partial", "blocked", "failed", "not_attempted"}


SEARCH_SORTS = {
    "auto",
    # Only meaningful together with `--rank bm25`; the substring path has no
    # score to order by and rejects it rather than silently ignoring it.
    "relevance",
    "updated_desc",
    "updated_asc",
    "title_asc",
    "title_desc",
    "occurred_desc",
    "occurred_asc",
}


REQUIRED_FIELDS = {
    "id",
    "schema_version",
    "type",
    "title",
    "created_at",
    "updated_at",
    "status",
    "sensitivity",
    "tags",
    "relations",
}


CUSTOM_ENTRY_FIELDS = REQUIRED_FIELDS | {
    "aliases",
    "type_version",
    "attributes",
    "provenance",
    "origin",
    "source",
    "unresolved_relations",
    "reviewed_at",
    "attachments",
    "previous_status",
    "deleted_at",
    "deleted_from",
    "purge_after",
    "deletion_reason",
}


COMPATIBLE_SCHEMA_VERSIONS = {8, 9, 10, 11, CURRENT_SCHEMA_VERSION}


# Schema 9 introduced the `type_version` / `attributes` envelope; schema 10
# only added the optional `provenance` map next to it, schema 11 only widened
# the relation object, and schema 12 only added optional aliases. Read and write
# paths therefore have to ask "does this
# entry use the envelope", not "is this the newest schema"; the second question
# rejected every schema 9 entry the moment 10 shipped, although nothing about
# its layout had changed.
ATTRIBUTE_ENVELOPE_VERSIONS = {9, 10, 11, CURRENT_SCHEMA_VERSION}


BUILTIN_CORE_FIELDS = CUSTOM_ENTRY_FIELDS


MAX_PROVENANCE_FIELDS = 128


PROVENANCE_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


# Machine-readable fence around body text that came off the network. It is an
# HTML comment so it stays invisible in every Markdown renderer and does not
# alter the personal note it sits next to. The `## Generated summary` heading
# is prose, is configurable, and was translated once already, so a reader
# cannot rely on it to tell fetched text from the user's own.
WEB_CONTENT_BEGIN = "<!-- noetrail:web-content -->"


WEB_CONTENT_END = "<!-- /noetrail:web-content -->"


ID_PATTERN = re.compile(r"^kn_[0-9a-f]{32}$")


TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


ATTACHMENT_ID_PATTERN = re.compile(r"^ka_[0-9a-f]{32}$")


ATTACHMENT_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


ATTACHMENT_PATH_PATTERN = re.compile(
    r"^vault/attachments/[0-9a-f]{2}/[0-9a-f]{64}\."
    r"(?:jpg|png|webp|gif|heic|avif)$"
)


LOCK_TIMEOUT_SECONDS = 10.0


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

# What a chat channel is asked to carry in one message, which is a smaller
# question than what the vault will store. Everything above this stays stored
# and readable from the vault; only the delivery is refused, with the size in
# the message so the refusal is actionable.
MAX_DELIVERABLE_ATTACHMENT_BYTES = 8 * 1024 * 1024


MAX_ATTACHMENTS_PER_ENTRY = 128


MAX_ALIASES_PER_ENTRY = 64


MAX_ALIAS_CHARS = 200


MAX_RECIPE_MINUTES = 7 * 24 * 60


MAX_MARKDOWN_IMPORT_BYTES = 2 * 1024 * 1024


MAX_MARKDOWN_IMPORT_FILES = 10_000


# A foreign vault is scanned whole, not just its Markdown: attachments, canvas
# files, and plugin data all have to be counted before anything is written, so
# the walk needs its own ceiling above the Markdown one.
MAX_VAULT_IMPORT_FILES = 50_000


# An entry's own frontmatter is small by construction: a fixed envelope plus
# one `attributes` object. These bounds only stop a pathological file from
# consuming unbounded time or stack in the block parser. They are generous
# against real entries, which sit two orders of magnitude below them, so no
# existing vault can be affected.
MAX_FRONTMATTER_LINES = 4_096


MAX_FRONTMATTER_LINE_CHARS = 262_144


# Block nesting beyond this is not something a Markdown tool produces for an
# entry; it is a stack-exhaustion attempt.
MAX_FRONTMATTER_DEPTH = 16


MAX_FRONTMATTER_KEY_CHARS = 256


MAX_VAULT_IMPORT_FRONTMATTER_LINES = 400


MAX_VAULT_IMPORT_LINE_CHARS = 8_192


MAX_OBSIDIAN_PROPERTY_KEYS = 128


MAX_OBSIDIAN_PROPERTY_ITEMS = 256


MAX_OBSIDIAN_PROPERTY_CHARS = 4_000


MAX_WIKILINK_TARGET_CHARS = 512


# Per entry. A generated index note can link to every other note in a vault;
# without a ceiling one such file would carry the whole graph in its
# frontmatter and make every read of it expensive.
MAX_VAULT_IMPORT_RELATIONS = 128


MAX_VAULT_IMPORT_UNRESOLVED = 64


# Report lists are capped and carry their own total, so a 10,000-note vault
# produces a readable report instead of a second copy of its file listing.
MAX_VAULT_IMPORT_REPORT_ITEMS = 50


OBSIDIAN_CONFIG_DIRECTORY = ".obsidian"


# Obsidian's own defaults that describe presentation or Obsidian Publish
# rather than note content. They are dropped and reported, never stored.
OBSIDIAN_PRESENTATION_PROPERTIES = frozenset(
    {"cssclasses", "cssclass", "publish", "permalink", "cover", "image"}
)


VAULT_IMPORT_SYSTEMS = {"obsidian": "obsidian", "basic-memory": "basic-memory"}


# How an attachment's bytes reached the vault. `attachment_inbox` is the
# interactive path an agent drives one image at a time; `vault_import` is the
# batch path, where the bytes came out of a foreign vault the user handed over
# as a whole. Both pass the same magic-byte and size checks before a blob is
# written, so the value records provenance and grants nothing.
ATTACHMENT_ORIGINS = {"attachment_inbox", "vault_import"}


ATTACHMENT_MEDIA_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/avif": "avif",
}


BOOKMARK_PAYLOAD_KEYS = {
    # The bookmark fetcher already reports `untrusted_web_metadata: true` for
    # its whole envelope. Accepting it here is what stops that statement from
    # being dropped at the vault boundary; before this the flag existed only
    # in the fetcher's reply and nothing about the stored entry recorded that
    # its title and description had come off a page.
    "untrusted_web_metadata",
    "provenance",
    "url",
    "canonical_url",
    "title",
    "aliases",
    "site_name",
    "authors",
    "published_at",
    "language",
    "page_description",
    "summary",
    "note",
    "tags",
    "reading_status",
    "bookmark_kind",
    "fetch_status",
    "relations",
    "unresolved_relations",
    "sensitivity",
}


UPDATE_PAYLOAD_KEYS = {
    "title",
    "aliases",
    "append",
    "replace_body",
    "sensitivity",
    "bookmark_kind",
    "record_experience",
    "experienced_at",
    "review",
    "rating",
    "experience_kind",
    "occurred_at",
    "occurred_precision",
    "source_url",
    "source_site_name",
    "source_description",
    "servings",
    "prep_minutes",
    "cook_minutes",
    "attributes",
}

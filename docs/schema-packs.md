# Declarative schema packs

Schema packs describe additional knowledge types without changing trusted
Python code. Packs can be installed, validated, listed, and described. The CLI
and Noetrail MCP support generic capture, update, search, and the common entry
lifecycle for pack-defined entries. Type discovery remains read-only.

## Locations and precedence

In the separated layout, bundled packs are discovered below
`<builtins-root>/packs/` and local packs below `<config-root>/packs/`. Each pack
is one directory containing `pack.yaml`:

```text
<config-root>/packs/books/
├── pack.yaml
└── book.md              optional inert body template
```

Pack IDs must be unique across both roots. A local pack never silently
overrides a bundled pack. In combined compatibility mode the single
`.knowledge/packs/` directory is read once and reported with source
`combined`.

The runtime rejects symbolic-link pack directories and manifests, template
paths outside their pack, hidden entries, non-directory children, duplicate
IDs, and unsupported format versions. Packs cannot choose vault paths.

## Minimal format

```yaml
format_version: 1
id: "books"
version: 1
title: "Books"
description: "Track books to read and personal reading history."
types:
  book:
    title: "Book"
    description: "A book the user reads or wants to read."
    template: "book.md"
    body_sections: ["Notes", "Quotes", "Personal review"]
    fields:
      author:
        type: "string"
        required: true
        searchable: true
        max_length: 500
      reading_state:
        type: "enum"
        values: ["wishlist", "reading", "read", "abandoned"]
        default: "wishlist"
        searchable: true
      pages:
        type: "integer"
        minimum: 1
      source_url:
        type: "url"
      topics:
        type: "array"
        items: "string"
        max_items: 20
```

The resulting qualified type ID is `books/book`. Pack and local type
components must match `[a-z0-9]+(?:-[a-z0-9]+)*`, are limited to 63 characters,
and form a qualified ID of at most 127 characters. Field IDs use lowercase
letters, digits, and underscores and must begin with a letter.

Bundled core packs may additionally bind a type to a reserved short entry ID
with `entry_type`, for example `entry_type: "bookmark"`. The local type key
must match that short ID. This capability exists only for trusted bundled or
combined-layout compatibility packs; a pack below the instance config root is
rejected if it attempts to claim a short built-in type. Ordinary extensions
always use qualified `pack/type` IDs.

## Supported fields

The first format deliberately supports a small, deterministic set:

| Type | Supported constraints |
| --- | --- |
| `string`, `text` | `min_length`, `max_length` |
| `integer`, `number` | `minimum`, `maximum` |
| `boolean` | literal boolean default |
| `date` | ISO 8601 date |
| `datetime` | ISO 8601 datetime with timezone |
| `url` | HTTP or HTTPS URL without embedded credentials |
| `enum` | non-empty unique `values` |
| `array` | scalar `items`, required `max_items`, unique values |

Every field may declare `required`, `searchable`, and a literal `default`.
Defaults must satisfy the same type and bounds. Arrays can contain strings,
integers, numbers, booleans, dates, datetimes, or URLs. Nested objects,
unbounded arrays, regular-expression validators, expressions, hooks, and
executable defaults are unsupported.

## Cross-field clauses

Three clauses state a relation between two fields of the same type. They are
declarations, not code: the loader only checks that they are well formed and
name a field that exists, and `noetrail validate` is what evaluates them.

| Clause | Meaning |
| --- | --- |
| `requires: {other: null}` | if this field is present, `other` must be too |
| `requires: {other: "value"}` | if this field is present, `other` must equal `value` |
| `host_of: "other"` | this string field must equal the hostname of the URL in `other` |
| `normalized: true` | this URL field must already be stored in canonical form |

```yaml
      read_at:
        type: "datetime"
        requires:
          reading_status: "read"
      domain:
        type: "string"
        max_length: 255
        host_of: "canonical_url"
```

The bundled `knowledge-core` pack uses all three, which is why the built-in
types need no type-specific Python: `noetrail validate` reads their rules from
the same declarations a third-party pack uses.

Pack `version` is the type-definition version recorded independently from the
vault's core `schema_version`. Migration declarations are not enabled yet;
changing a version does not rewrite existing data.

## Restricted YAML and templates

`pack.yaml` uses a dependency-free data-only YAML subset:

- two-space mapping indentation;
- mapping keys plus scalar values;
- inline JSON syntax for strings, arrays, and objects;
- no anchors, aliases, tags, merges, block scalars, or duplicate keys;
- bounded manifest size, line length, pack count, type count, and field count.

Unknown keys fail validation. This is intentional: a typo must not silently
change the meaning of a personal-data schema.

An optional template must be an existing UTF-8 `.md` file below the pack
directory. It is inert text. It cannot contain expressions that the registry
executes, invoke tools, grant permissions, access a network, or authorize vault
changes. Agent-facing descriptions are installed local data, not authority.

## Inspecting the registry

Start a new local pack with `noetrail schema init my-pack --type item`. By
default it creates `<config-root>/packs/my-pack/pack.yaml`; `--output` selects
an existing parent directory for an uninstalled candidate. The command refuses
an existing destination and writes a minimal, valid, data-only manifest.

Validate a candidate before installation with
`noetrail schema validate-pack /path/to/my-pack`. This applies the complete
manifest, template, field, bound, cross-field, path, and symlink checks and
returns each generated entry JSON Schema. It neither installs the pack nor
reads or mutates the vault. This closes the previous authoring loop where a
pack had to be copied into the live config before the loader could explain its
first error.

With a source checkout:

```sh
noetrail schema validate
noetrail schema list
noetrail schema explain books/book
```

The same root flags documented in [Layout](layout.md) can precede `schema`.
`explain` includes the effective generated JSON Schema. The MCP server exposes
the equivalent read-only `list_types` and `describe_type` operations. It also
derives the custom portions of the `capture`, `update`, and `search` input
schemas from the registry at process start. A host may prefix these canonical
names with its configured server alias.
Installing or changing a pack requires an explicit MCP restart; an active
process never changes its tool contract in the middle of a session.

Every CLI operation and MCP startup validates the active registry first. An
invalid pack therefore fails before a vault mutation or attachment copy can
begin.

## Capturing and searching pack-defined entries

Attributes are supplied as one JSON object. This keeps typed values intact and
avoids shell-specific parsing rules:

```json
{
  "author": "Ursula K. Le Guin",
  "reading_state": "reading",
  "topics": ["anarchism", "science fiction"]
}
```

```sh
noetrail capture \
  --type books/book \
  --title "The Dispossessed" \
  --attributes-file book.json \
  --text "A personal note about the book."
```

Missing literal defaults are inserted at capture. If body text is empty, the
static template is used, falling back to generated headings from
`body_sections`. The resulting entry is stored below
`vault/custom/books/book/` with `type_version` and `attributes` in frontmatter.

Normal text search indexes the title, body, tags, and only attributes declared
`searchable`. Exact field filters require a qualified type and a JSON value:

```sh
noetrail search "Le Guin" --type books/book
noetrail search "" --type books/book \
  --attribute-filter 'reading_state="reading"'
```

Update patches use an `attributes` object. Values replace individual fields;
`null` removes an optional field. The complete resulting attributes object is
validated before the revision-checked atomic write:

```json
{"attributes":{"reading_state":"read","pages":341}}
```

```sh
noetrail update kn_... \
  --patch-file update.json \
  --expected-revision sha256:...
```

The same operations are available through MCP with `attributes` on capture and
update and `attribute_filters` on search. MCP writes still delegate to the CLI,
so both interfaces apply the same Registry validation, storage paths, locks,
and revision checks.

Generic relations, tags, attachments, review reads, trash, restore, and vault
validation work without type-specific branches. Search results return only
searchable attributes; inspect a selected entry to read its full attributes
and body.

## Trust and installation

Only an administrator or local user should install packs into the read-only
configuration mount. A webpage, imported note, chat message, or agent may
suggest a pack but cannot approve or install it. Inspect third-party pack
descriptions and templates as untrusted prose even though the format contains
no executable code.

Keep instance-specific packs out of the private data root. If a pack contains
personal categories or descriptions, keep it in private instance
configuration rather than publishing it with the application.

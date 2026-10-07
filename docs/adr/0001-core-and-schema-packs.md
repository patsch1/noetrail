# ADR 0001: Stable core and declarative schema packs

- Status: accepted
- Date: 2026-08-01
- Roadmap phase: 1

## Context

The current vault has a strong storage and security core, but domain knowledge
is encoded repeatedly in Python constants, CLI arguments, MCP input schemas,
JSON Schema, templates, tests, skills, and prose documentation. Adding recipes
demonstrated that a new type currently requires coordinated edits across all
of those layers.

An independently usable project must let users model domains such as books,
plants, collections, workouts, or travel without editing trusted Python code.
The extension mechanism must retain deterministic validation, safe paths,
revision checking, migration previews, and the narrow MCP security boundary.

## Decision proposal

Adopt a stable core entry envelope and a declarative schema-pack registry.

### Stable core envelope

The application exclusively owns these concepts:

- `id`
- `schema_version`
- `type`
- qualified type-definition version
- `title`
- `created_at` and `updated_at`
- lifecycle `status`
- `sensitivity`
- `tags`
- stable-ID relations and unresolved relation references
- attachments and attachment provenance
- import provenance and origin
- deletion metadata while in trash
- revision derivation, locks, and atomic writes

Packs cannot redefine, shadow, remove, or weaken these fields.

### Qualified custom types

Built-in short type names remain reserved during the compatibility period.
Custom types use a qualified identifier containing the pack ID and local type
ID, written as `books/book`. Each component must match
`[a-z0-9]+(?:-[a-z0-9]+)*`, may contain at most 63 characters, and the complete
qualified ID may contain at most 127 characters.

Entries record the type-definition version independently of the core schema:

```yaml
type: "books/book"
type_version: 1
attributes: {"author":"Ursula K. Le Guin","reading_state":"reading"}
```

Identifiers are portable, lowercase, bounded, path-independent, and unique
within one installation.

### Namespaced attributes

Pack-defined values live under one JSON-compatible `attributes` mapping. This
prevents collisions with future core fields and gives generic update, search,
and migration code one bounded surface.

The first version should support only a deliberately small field system:

- string and long text;
- integer and number with bounds;
- boolean;
- date and timezone-aware datetime;
- URL;
- enum;
- bounded arrays of supported scalar types.

Arbitrary nested schemas, dynamic expressions, executable defaults, and custom
regular-expression engines are not required for the first alpha.

### Pack format

A minimal pack is a YAML file using the project's restricted, data-only YAML
subset. A provisional example is:

```yaml
format_version: 1
id: "books"
version: 1
title: "Books"
description: "Track books to read and personal reading history."
types:
  book:
    title: "Book"
    description: "A book the user owns, reads, or wants to read."
    fields:
      author:
        type: "string"
        required: true
        searchable: true
      reading_state:
        type: "enum"
        values: ["wishlist", "reading", "read", "abandoned"]
        default: "wishlist"
        searchable: true
      pages:
        type: "integer"
        minimum: 1
      isbn:
        type: "string"
    body_sections: ["Notes", "Quotes", "Personal review"]
```

The authoritative format is intentionally smaller than general JSON Schema.
The registry generates standard JSON Schema and MCP input-schema fragments
from the resolved pack definitions.

### Storage

Packs do not provide filesystem paths. The application derives custom entry
locations from validated identifiers, provisionally:

```text
vault/custom/<pack-id>/<type-id>/
```

Built-in packs are read-only application resources. Local custom packs live in
a separate instance-config root by default, not in the private data root. Pack
files are discovered only below explicit built-in and local pack roots.
Symlinks, traversal, duplicate identifiers, and unsupported versions fail
before the vault is read for mutation.

The data root contains `vault/`, `trash/`, import workspaces, attachments, and
derived runtime state. Desktop installations may follow XDG config and data
locations. The ZeroClaw deployment may mount application resources and
instance configuration read-only while mounting only the data root from the
PVC. The existing combined `--root` layout remains a compatibility mode during
the transition.

### Agent semantics

Descriptions and field help are data supplied by an explicitly installed
pack. The MCP server exposes read-only discovery operations:

- `knowledge__list_types`
- `knowledge__describe_type`

Agents load one relevant definition on demand. Pack content cannot grant tool
permissions, authorize network access, invoke a shell, or override the core
workflow. Installing a third-party pack remains an explicit trusted local
administrative action.

### Generic operations

Capture and update accept an `attributes` object. Search accepts bounded field
filters against fields declared searchable. The registry validates the same
payload for both CLI and MCP so the two interfaces cannot drift.

Security-sensitive workflows may keep narrow specialized tools. In particular,
bookmark fetching remains isolated from the vault, and attachment ingestion
continues to accept files only from the fixed channel inbox.

### Versioning and migration

Core `schema_version` and per-entry `type_version` evolve independently.
Pack migrations must be declarative and previewable. The first vertical
prototype supports:

- add an optional field;
- set a literal default when a field is absent;
- rename a field;
- rename or map enum values;
- map existing enum values.

Moving legacy type-specific values into `attributes` is needed before the
first public alpha, but is implemented as a later reviewed built-in migration
rather than part of the initial custom-type spike.

Schema packs may declare simple body sections and may include one optional
static Markdown template per type. Templates are inert text assets below the
validated pack root; they cannot execute expressions, tools, or code.

Removing populated data, merging ambiguous values, executing code, or deriving
events and relations requires a separate explicitly reviewed migration and is
not automatically inferred.

## Consequences

Benefits:

- users can create useful domain types without changing Python;
- CLI, MCP, validation, and generated JSON Schema share one registry;
- custom fields cannot collide with core integrity metadata;
- the same definitions can later generate forms, documentation, and indexes;
- executable plugin risk is avoided for the initial extension model.

Costs:

- the current monolithic CLI and static MCP tool list need refactoring;
- core and pack compatibility must be tested as a version matrix;
- dynamic tool schemas require MCP/session restart after pack changes;
- moving current built-in fields into `attributes` may require a larger
  pre-alpha migration or a documented compatibility layer.

## Rejected alternatives

### Let users edit the global entry JSON Schema directly

Rejected because it does not define agent semantics, search facets, storage,
trust boundaries, or safe migrations, and it would leave CLI and MCP behavior
out of sync.

### Python plugins or executable hooks

Rejected for the initial project because they greatly expand the supply-chain,
filesystem, secret, and agent-authority boundary.

### Keep adding every domain type to the core

Rejected because it makes the project maintainer the bottleneck for personal
domain models and repeats the current cross-file duplication.

### Store arbitrary custom fields at top level

Rejected because pack fields could collide with later core metadata and make
generic validation and migrations harder to reason about.

## Accepted implementation defaults

1. Custom type IDs use the qualified `pack/type` notation and the bounded
   identifier grammar above.
2. Built-in type-specific fields eventually move below `attributes` before the
   first public alpha, using explicit compatibility migrations.
3. Installed built-ins, instance configuration, and private data use separate
   roots; the current combined root remains temporarily supported.
4. Packs support simple body sections and an optional inert Markdown template.
5. The first migration engine supports adding optional fields or literal
   defaults, renaming fields, and mapping enum values.

## Validation spike

Before accepting this ADR as implemented, build one synthetic `books/book`
vertical slice that proves:

- pack loading and validation;
- safe derived storage;
- capture and update of typed attributes;
- field-filtered search;
- generic CLI and MCP parity;
- generated JSON Schema;
- revision, relation, attachment, trash, and migration protections;
- failure before mutation for invalid or malicious pack definitions.

# Five-minute quickstart

This walkthrough creates a disposable Noetrail instance from a clean checkout.
It uses only synthetic data and never touches a real vault.

## 0. The one-command version

If all you want is a working instance and the configuration line for an MCP
client, this is the whole thing:

<!-- docs-check: skip - uvx installs from a package index over the network -->

```sh
uvx --from 'noetrail==0.10.0a4' noetrail quickstart
```

`0.10.0a4` is on PyPI. The command pins the alpha version explicitly. See
[installation](installation.md#install-single-user-own-machine) for
virtual-environment and TestPyPI alternatives. From a checkout the same command is
`.venv/bin/noetrail quickstart`, which is what the rest of this page uses. It
creates `~/noetrail/data` and `~/noetrail/config` (override with `--path`),
writes three sample entries, and prints the MCP server block. With `uvx`, use
the [uvx client configuration](integrations/mcp-clients.md#install-and-get-the-configuration-block)
so the client can launch the package without a global `noetrail-mcp` command.

## 1. Install the local build

Python 3.11 or newer is required:

<!-- docs-check: skip - installs from the network; the doc runner substitutes a local entry point -->

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Create a temporary working directory and let `quickstart` fill it:

```sh
DEMO_ROOT="$(mktemp -d)"
.venv/bin/noetrail quickstart --path "$DEMO_ROOT" --json
```

The data root now contains `vault/`, `trash/`, and private import workspaces.
The config root contains an empty local-pack directory. `quickstart` is
idempotent: running it again on the same path adds nothing.

Every command below repeats the two roots explicitly, because a real
installation keeps them apart from the program:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  review
```

## 2. Inspect a declarative type

Noetrail ships a synthetic `books/book` example pack. Inspect the type without
loading every schema into an agent prompt:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  schema explain books/book
```

## 3. Import Markdown safely

Create a synthetic source below the protected raw-import root:

```sh
mkdir -p "$DEMO_ROOT/data/imports/raw/markdown"
printf '%s\n' \
  '# Small systems worth reading about' \
  '' \
  'A synthetic Markdown note used only by the quickstart.' \
  > "$DEMO_ROOT/data/imports/raw/markdown/reading-list.md"
```

Preview first. This returns a structural report and writes nothing:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  import markdown --source markdown
```

Apply the same preflighted operation explicitly:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  import markdown --source markdown --apply
```

Run the apply command again to see the unchanged source reported as
`already_imported` instead of duplicated.

## 3b. Import an Obsidian vault

Copy a vault below the same protected raw-import root. This synthetic one has
a wikilink, an unresolvable link, an inline tag, and a daily note:

```sh
mkdir -p "$DEMO_ROOT/data/imports/raw/obsidian-vault/Daily"
printf '%s\n' \
  '---' \
  'title: Reading index' \
  'tags:' \
  '  - index' \
  'aliases:' \
  '  - Home' \
  '---' \
  '' \
  '# Reading index' \
  '' \
  'Started from [[Daily/2026-08-01]] and still owe [[A note I have not written]].' \
  'Filed under #inbox/to-read.' \
  > "$DEMO_ROOT/data/imports/raw/obsidian-vault/Index.md"
printf '%s\n' \
  'Read two chapters and linked them back to [[Index]]. #daily' \
  > "$DEMO_ROOT/data/imports/raw/obsidian-vault/Daily/2026-08-01.md"
```

Preview, then apply:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  import obsidian --source obsidian-vault

.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  import obsidian --source obsidian-vault --apply
```

The report names what was converted and what was not: `counts` for notes,
relations, unresolved links and attachments, `not_imported` for canvas files,
Dataview blocks, Templater expressions, dropped presentational properties and
embeds that could not become attachments, and `rejected` for symbolic links
and frontmatter the importer refuses to guess at. `unresolved_link_targets`
lists the links whose targets are not in the vault; they are kept on the entry
as unresolved relations rather than dropped.

A Basic Memory project directory is imported the same way with
`import basic-memory --source <directory>`. See
[Importing notes](importing.md) for the full mapping.

## 4. Capture and search a pack-defined entry

Create a small JSON attribute payload:

```sh
printf '%s\n' \
  '{"author":"Ada Example","reading_state":"wishlist","topics":["local-first systems"]}' \
  > "$DEMO_ROOT/book.json"
```

Capture a synthetic book:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  capture --type books/book \
  --title "The Small System Atlas" \
  --attributes-file "$DEMO_ROOT/book.json" \
  --text "A fictional book used only by the synthetic quickstart."
```

Search across titles, bodies, tags, and searchable typed attributes:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  search local-first
```

A multi-word question works the same way, because `search` runs a literal
match and a BM25 ranking together and returns both:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  search "atlas local-first fictional"
```

Restricting it to the literal half returns nothing, because those three words
never appear adjacently — which is what the default exists to avoid:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  search "atlas local-first fictional" --rank substring
```

The ranked half is answered from a scan unless an index exists; build one and
the answer stays identical while the query gets faster:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  index rebuild

.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  index status
```

The index lives under the config root, never under the data root, and
`index drop` removes it at any time. See
[Limits and scaling](limits.md) for what it costs and what it saves.

Get complete high-level counts without returning entry titles or bodies:

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  inventory
```

## 5. Validate the result

```sh
.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  validate

.venv/bin/noetrail \
  --data-root "$DEMO_ROOT/data" \
  --config-root "$DEMO_ROOT/config" \
  doctor
```

Delete `DEMO_ROOT` when finished. A real instance should instead use private
storage with a tested backup and restore path.

Next, read [installation and lifecycle](installation.md),
[schema packs](schema-packs.md), and [privacy boundaries](privacy.md).

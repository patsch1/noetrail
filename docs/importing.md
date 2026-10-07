# Importing notes

Imports are trusted local maintenance operations. Raw exports remain private,
the production agent does not receive import tools, and external content never
authorizes follow-up actions.

## Storage boundary

Keep original and derived files below the private data root:

```text
imports/
├── raw/<source>-<date>/     unchanged source export
└── work/<source>-<date>/    derived local working files
```

Both paths are excluded from Git. Back them up according to the same privacy
policy as the vault. Never rewrite `imports/raw/` to make an importer succeed.

## Generic UTF-8 Markdown

Noetrail can import a `.md` file or a directory recursively. The source must
resolve below `<data-root>/imports/raw/`; symlinks, non-UTF-8 input, empty
files, NUL bytes, files larger than 2 MiB, and runs above 10,000 Markdown files
are rejected before any entry is written.

Dry-run is the default:

```sh
noetrail --data-root /srv/noetrail-data --config-root /etc/noetrail \
  import markdown --source markdown-export
```

Use `--limit 10` for an initial representative trial. The report contains
source paths, derived titles, planned entry IDs, skips, and conflicts, but no
note bodies. Review it before applying:

```sh
noetrail --data-root /srv/noetrail-data --config-root /etc/noetrail \
  import markdown --source markdown-export --limit 10 --apply
```

For the complete run, omit `--limit`. The importer:

- preserves the Markdown body and first level-one heading;
- falls back to a readable filename-derived title;
- creates `note` entries with `status: unreviewed`;
- records source system, original path, stable source ID, import timestamp, and
  source-content SHA-256;
- derives a stable entry ID from the raw relative path;
- skips an unchanged repeated import;
- reports changed raw input as a conflict;
- preflights every selected file and rolls back newly created entries if a
  later write fails.

It does not copy linked attachments or infer relationships. For a vault whose
links and tags carry meaning, use `import obsidian` or `import basic-memory`
below instead.

## Obsidian

`noetrail import obsidian --source <directory>` reads a copy of an Obsidian
vault placed below `<data-root>/imports/raw/`. It has the same contract as the
generic importer — dry-run by default, `--apply` to write, stable entry IDs
derived from the vault-relative path, a source content hash, and a complete
rollback if any write fails — plus the vault-specific conversions below.

### What it maps

| Obsidian | Noetrail |
| --- | --- |
| `[[Note]]`, `[[Note\|display]]`, `[[Note#Heading]]`, `[[folder/Note]]` | `related_to` relation to the imported entry |
| `[[Alias]]` where `Alias` is in another note's `aliases` | `related_to` relation to that note |
| A link whose target is not in the vault | `unresolved_relations` entry, reference kept verbatim |
| `![[image.png]]` | content-addressed attachment with `origin: vault_import` |
| Frontmatter `title` | entry `title` (falls back to the first `#` heading, then the filename) |
| Frontmatter `tags` (list or comma-separated) and inline `#tag` | entry `tags`, normalized and de-duplicated |
| Frontmatter `aliases` | searchable entry `aliases`, also retained in `source.aliases` for provenance |
| Any other user property | `source.frontmatter`, verbatim |
| Daily notes, folders, attachment folders | ordinary notes and paths; no special casing |

Obsidian's `[[Note]]` is untyped, so `related_to` — the symmetric predicate —
is the only honest mapping. Note names are matched case-insensitively, by
relative path first and by bare filename second; a name that more than one
note uses stays unresolved rather than resolving to whichever file was seen
first. A link target is only ever matched against the scanned file listing, so
`[[../../etc/passwd]]` becomes an unresolved reference and never a file read.

The built-in `note` type declares no typed attributes, so there is no schema
slot for a user property such as `status: draft`. Those are preserved under
`source.frontmatter` rather than being invented into `attributes`. To get
typed attributes instead, define a [schema pack](schema-packs.md) and move the
entries deliberately after the import.

### What it reports instead of converting

The report's `not_imported` section names, per run:

- `canvas` — `.canvas` files, which are JSON diagrams, not notes;
- `unsupported_files` and `unsupported_embeds` — non-Markdown, non-image files
  and the embeds pointing at them;
- `attachment_problems` — an embedded image that is empty, oversized, or not
  actually one of the supported image formats;
- `dropped_properties` — presentational and Obsidian Publish properties
  (`cssclasses`, `publish`, `permalink`, `cover`, `image`) and any property
  whose value is not a scalar or a flat list;
- `dataview_blocks`, `templater_expressions`, `core_template_placeholders` —
  syntax that stays in the body as text but stops doing anything.

`ignored` records that a `.obsidian/` configuration directory was found and
skipped, along with any other hidden entry. `unresolved_link_targets` lists
every link that did not resolve.

### What it refuses

Refusals are listed under `rejected`; the note is skipped and the rest of the
import proceeds. Pass `--strict` to turn any rejection into a failed run that
writes nothing.

- A symbolic link anywhere in the tree. Nothing behind it is opened.
- A file that is not valid UTF-8, is empty, contains a NUL byte, or exceeds
  2 MiB.
- Frontmatter using YAML anchors, aliases, tags, merge keys, block scalars,
  nested mappings, tabs, control characters, or duplicate keys.

That last one is deliberate. Noetrail parses a small data-only subset of YAML
rather than shipping a full YAML implementation, and the alternative to
refusing an anchor is guessing at a value the reader cannot see next to it.
The refusal names the property block's line number, so the file can be fixed
in the source vault and re-imported.

### Limits

A foreign vault is untrusted input, so everything is bounded before anything
is written: 50,000 files scanned, 10,000 Markdown notes, 2 MiB per note,
20 MiB per embedded image, 400 property lines and 128 properties per note,
128 relations and 64 unresolved references per entry. `--limit N` takes the
first N notes in path order for a representative trial run.

Each report section lists at most 50 items; `counts` carries the totals, so a
large vault produces a readable report rather than a second copy of its file
listing.

## Basic Memory

`noetrail import basic-memory --source <directory>` reads the Markdown files
of a [Basic Memory](https://github.com/basicmachines-co/basic-memory) project.
It reads the files only, never the SQLite database: that database is a
secondary index derived from the files.

| Basic Memory | Noetrail |
| --- | --- |
| Frontmatter `title` | entry `title`, and a name links resolve against |
| Frontmatter `permalink` | `source.permalink`, and the stable source ID |
| Frontmatter `type` | `source.original_type` |
| Frontmatter `tags` | entry `tags` |
| `## Observations` — `- [category] text #tag` | body text unchanged; categories in `source.observation_categories`, tags in entry `tags` |
| `## Relations` — `- relation_type [[Target]]` | relation with that predicate when the instance configures it, otherwise `related_to` |
| `[[Target]]` with no matching note | `unresolved_relations` entry |

Because `permalink` is the documented stable identifier, moving or renaming a
file that carries one does not create a second entry.

An unconfigured relation type — Basic Memory's vocabulary is open, Noetrail's
is not — is substituted with `related_to` and listed under
`not_imported.substituted_relation_types`. Add the predicate to
`<config-root>/relation-types.yaml` and re-import to keep it. A line inside
either section that does not match the documented shape is left in the body
untouched and counted, never reinterpreted.

## Notion

Inspect a small real export before writing conversion rules. Account for:

- nested pages and database rows;
- page IDs, properties, timestamps, and exported filenames;
- internal page links;
- CSV tables and attachment directories.

Keep the Notion page ID as the source ID. Do not guess database-property
semantics or flatten unsupported blocks silently.

## Joplin

Inspect notebook hierarchy, note IDs, tags, timestamps, Markdown resources,
internal note links, and shared `:/resource-id` references. A source-specific
converter must preserve or explicitly report every referenced resource.

## Trial report and completion criteria

A tailored import report should aggregate:

- created and skipped entries;
- duplicates and changed raw sources;
- unresolved internal links;
- missing or unsupported attachments;
- unsupported blocks and every known lossy conversion.

Do not include full sensitive bodies in a report. An import is complete only
when repeated execution creates no duplicates, all supported entries validate,
raw data remains unchanged, and unresolved loss is explicitly documented.

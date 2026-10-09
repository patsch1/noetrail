# Changelog

All notable user-visible changes will be documented here. The project follows
[Semantic Versioning](https://semver.org/) with additional pre-`1.0` rules in
[the versioning policy](docs/versioning.md).

## Unreleased

## 0.10.0a6 - 2026-10-09

- omit `delivery_path` from `get_attachment` text and structured content when
  an outbox marker template is configured; the complete `delivery_marker`
  remains the transport field, avoiding a second bare image path that clients
  can load into model requests;
- preserve generic outbox `delivery_path` replies without a template and the
  standard MCP image-content response without an outbox;
- record live confirmation of Discord v0.8.4 `discord_files` staging and
  `capture` followed by `add_attachment`, and distinguish the Noetrail output
  regression from the deployed client's complete marker-normalization path.

## 0.10.0a5 - 2026-10-09

Documentation and deployment-guidance release; runtime behavior and core schema
12 are unchanged.

- use absolute FILE markers for chat-confirmed inline image delivery on
  ZeroClaw Discord v0.8.4, keeping relative IMAGE markers specific to Matrix;
- mark Discord inbound image staging under `discord_files` as unverified and
  document a synthetic upload, staging, listing and attachment smoke test.

## 0.10.0a4 - 2026-10-09

- add revision-checked CLI and MCP bookmark refresh from isolated fetcher
  envelopes, filling missing page metadata with web provenance while preserving
  existing values, titles, tags and bodies;
- keep bookmark refresh under normal ZeroClaw session approval so a permitted
  batch does not require a separate confirmation for every bookmark.
- add `has_attachment` to CLI and MCP search/retrieve, with explicit attachment
  counts in tool descriptions and guidance for finding images across all types;
- document ZeroClaw v0.8.4 Discord's `discord_files` inbox and absolute PHOTO
  delivery marker, keeping relative IMAGE markers specific to Matrix.

## 0.10.0a3 - 2026-10-08

- add conservative title/alias typo candidates after an empty hybrid lexical
  and word-form search; labels and observed terms distinguish hints from evidence;
- let CLI and MCP `retrieve` combine up to four supplied query strings with
  reciprocal rank fusion, per-query match origins and shared result/body budgets;
- teach the ZeroClaw knowledge prompt to batch translated or corrected variants
  while counting each string toward its existing four-query budget.

## 0.10.0a2 - 2026-10-07

Documentation release; runtime behavior and core schema 12 are unchanged.

- clarify the public README, installation scope, AI authorship, retrieval
  limits, and privacy boundaries;
- add a synthetic alpha-pilot kit and an anonymous observation template;
- accept a validated release date in the release-consistency check while
  rejecting ambiguous headings and invalid dates.

## 0.10.0a1 - 2026-10-07

GitHub prerelease, PyPI and TestPyPI published on 2026-10-07 after maintainer
approval. The package remains an alpha.

### Added

- published identical alpha distributions on GitHub, PyPI and TestPyPI,
  with explicit alpha-version pins in installation instructions;

- public CodeQL analysis with reviewed synthetic-secret test findings and
  clearer test assertions, parser exits and HTTP shutdown/logging intent;

- a public source snapshot with disclosed AI authorship and a fresh Git history;
  earlier development history and operational conversations remain private;

- explicit source-scoped absence wording and full candidate reads after an
  unsuccessful inferred type restriction in the ZeroClaw knowledge prompt;
  delegated answers must preserve bounded-search uncertainty and aggregate
  requests must not become unsolicited title listings;

- conservative word-form candidates when a filtered hybrid search is empty:
  bounded endings and grounded compound splits can recover missing entries;
  `match_kind: word_form` distinguishes tentative candidates in search and
  retrieve, including MCP, and opt-in explanations retain source provenance;

- bounded query reformulation in the always-loaded ZeroClaw knowledge prompt:
  spelling corrections, translations and less restrictive inferred filters help
  find existing notes before reporting an unsuccessful search;

- CI-gated ZeroClaw deployment candidates for a staged, schema-aware cluster rollout;

- opt-in bounded search excerpts with provenance (`search --explain`, MCP
  `explain`), plus a 100-question multilingual retrieval diagnostic suite;
- `candidates` / `find_candidates` suggestions and conservative, preview-first
  `merge` / `merge_entries` with a revision-bound plan, source retention in
  trash and rollback on write exceptions;
- a separated-root restore test covering a custom pack, saved view, relation
  and attachment, and a concrete small-alpha pilot protocol;
- cooperative 30-second deadlines for in-process MCP reads, returning
  `read_timeout` while leaving subsequent requests usable;
- corrected nonempty tokenless searches: hybrid keeps the literal predicate;
  BM25 returns no candidates instead of listing every entry;
- performance measurements now name their ranking mode accurately, include
  hybrid and multiword queries, and actually skip omitted agent turns.

- schema version 12 adds bounded, searchable `aliases` to every entry type.
  Aliases are explicit alternative names, not agent-invented topical synonyms;
  captures and revision-protected updates can add them, and the 11-to-12
  migration changes only the schema marker without guessing new data;
- `noetrail retrieve` and the MCP `retrieve` tool combine a filtered search
  with bounded full-entry reads in one process. The response reports per-body
  and aggregate truncation, so agents can reduce retrieval round trips without
  silently treating partial content as complete;
- declarative, read-only saved views in `views.yaml`, exposed through
  `noetrail view list`, `noetrail view run`, `list_views`, and `run_view`.
  Views reuse the existing search filters and accept no hooks, paths, network
  instructions, or mutations;
- `noetrail schema init` scaffolds a minimal inert schema pack and
  `noetrail schema validate-pack` applies the complete pack trust boundary
  before installation, including generated JSON Schema validation;
- [How this project was built](docs/ai-authorship.md), stating that Noetrail is
  vibe-coded: its code, tests, and documentation were written by AI coding
  agents directed by one human maintainer. Until now that was only inferable
  from `AGENTS.md` and the commit trailers. The page says what is verifiable,
  which gates stand in for a person typing every line, and what the disclosure
  does not change — responsibility, the security commitments, and how a report
  is judged. `README.md`, `CONTRIBUTING.md`, and `SECURITY.md` state it in
  their own terms and link there;
- discovery-era MCP `2026-07-28` support alongside the existing
  `2024-11-05` initialize handshake. Both knowledge and bookmark-fetch servers
  now advertise supported versions through `server/discover`, validate the
  required per-request metadata, and return complete/cache result metadata
  without changing legacy responses. Protocol smoke profiles cover ZeroClaw,
  a desktop host, an editor host, and the discovery-era wire contract;
- portable agent skills now use canonical MCP names and standard image content
  instead of assuming ZeroClaw's `knowledge__*` alias or Matrix delivery
  markers. `deploy/zeroclaw/AGENTS.md` remains the explicit host overlay, so
  the productive deployment keeps its routing and channel behavior while
  other MCP hosts receive no ZeroClaw-specific instructions;
- `get_attachment` returns an image an entry already holds, as MCP image
  content a chat channel can display. Until now a photo could be stored and
  never seen again: nothing in the tool surface returned attachment bytes, so
  an agent asked to show a saved photo could only report that it could not.
  The tool names an `attachment_id` from the entry's own `attachments`, so it
  cannot address a file by path or reach a blob no entry references, and it
  verifies the recorded size, digest and media type against the file before
  returning a byte. An image above `MAX_DELIVERABLE_ATTACHMENT_BYTES` (8 MiB)
  is refused with its size rather than truncated, and stays stored;
- `noetrail-mcp --attachment-outbox` copies that image into a directory
  the host can send from, and reports it as `delivery_path`. Returning
  MCP image content is the portable answer, but a host that uploads a
  file only when the reply names a path inside its own workspace cannot
  use it, and the vault is not in that workspace. The copy is named by
  content digest, so asking twice writes one file. Unset, nothing is
  written and the image content alone is returned. With an outbox the
  bytes are left out of the response entirely: the host sends the file,
  so the pixels have no business in the model's request, and putting
  them there made the provider refuse it outright with "Stream must be
  set to true". A host may now additionally configure the bounded
  `--attachment-delivery-marker-template` with one `{path}` placeholder;
  `get_attachment` then returns a ready-to-copy `delivery_marker` rather
  than requiring an agent to reconstruct host syntax. The template remains
  an explicit integration setting, so portable clients and skills still
  assume no ZeroClaw- or Matrix-specific marker. A host may additionally set
  `--attachment-delivery-marker-root` to make `{path}` relative to a validated
  workspace containing the outbox. This keeps outbound markers visible to
  clients that otherwise consume absolute image references as model input;

- `noetrail retype <id> --to <type>` corrects an entry filed under the wrong
  type. Every type added after a vault exists leaves entries behind under the
  type they were given -- recipes captured before the `recipe` type existed
  stayed notes -- and `update` refuses to touch `type` on purpose, because the
  type decides both the storage path and which attributes are legal. Retyping
  revalidates the attributes against the target type, applies its defaults,
  and moves the file with a single rename; the stable ID, creation time,
  relations, attachments, tags, status, sensitivity and body are untouched, so
  a relation pointing at the entry still resolves. It previews by default like
  `migrate` and `purge`, refuses a target whose required fields cannot be
  satisfied, refuses to discard attributes the target does not define unless
  `--drop-unsupported` says the loss is intended, and takes
  `--expected-revision`. It is a CLI operation only: retyping is a structural
  change to personal content, which is the kind of thing the agent boundary
  deliberately does not reach;

- a search whose type filter matches nothing now reports what lifting that
  filter would find. `without_type_filter` carries the count and the types of
  the entries the same query matches without the type restriction, and appears
  only when a type filter was given, the result was empty, and something else
  did match. This closes a way for a correct search to produce a wrong answer:
  asked which recipes were saved, an agent searched `type: recipe`, got
  `total: 0`, and reported that there were none while five recipes sat in the
  vault as notes -- captured before the `recipe` type existed, a window every
  future type will have. The extra pass runs only on that empty result, so an
  ordinary search costs nothing;

- an optional BM25 ranking mode. `search --rank bm25` scores against a
  persisted inverted index built from titles, tags, bodies and every field a
  type declares `searchable`. It is opt-in and there is no index until
  `noetrail index rebuild` creates one; substring stays the default, because
  it can match inside a word and a silent change of ranking would change what
  every stored query means. Measured on a 2-core container at 10 000 entries:
  608 ms substring, 140 ms indexed BM25, 1 248 ms BM25 without an index; the
  index costs 9.4 MB and 2.3 s to build, and roughly doubles the cost of a
  `capture`. Retrieval quality on the synthetic set in
  `tools/retrieval_set.json` -- a self-built set, not an external benchmark --
  is Recall@1 0.28 and MRR 0.30 for substring against 0.83 and 0.90 for BM25;
  substring returns nothing at all for 14 of its 20 queries, which is what
  literal substring matching means for a query of more than one word;
- the index is a cache, never an authority. Every read checks it against the
  vault first and falls back to a full scan on any mismatch, corruption is
  detected by a payload digest before the first posting is read, and
  `noetrail index status --verify` re-derives it from the Markdown. `doctor`
  reports its state as a warning, never an error;
- a declarative interface for an external embedding provider
  (`docs/embeddings.md`, `search --rerank-vectors`). Noetrail opens no socket
  and writes no vectors: the caller names a JSONL sidecar explicitly, and
  vectors only reorder the lexical candidates, never add one -- so every
  result stays explainable by words that are in the file. No provider is
  bundled, which is what keeps the dependency count at zero and the pack
  format non-executable;
- `noetrail import obsidian` and `noetrail import basic-memory`. The Obsidian
  importer maps wikilinks to typed relations (unresolvable targets become
  unresolved relations, which already existed), embeds to content-addressed
  attachments, and inline `#tag`s and frontmatter to the envelope; it reports
  what it could not map -- Canvas files, Dataview, Templater -- instead of
  swallowing it, and rejects symlinks, non-UTF-8 input and pathological
  frontmatter. Obsidian properties are parsed by their own parser with the
  same rejection list as the pack parser, so the pack parser's hardening was
  not widened to accommodate foreign input;
- `noetrail quickstart`, which creates an instance, writes a few synthetic
  entries and prints the MCP client configuration block, so trying the project
  is one command. `docs/installation.md` now presents Kubernetes as one of
  several ways to run it rather than a prerequisite;
- schema 11 records how long a relation holds and which relation replaced it.
  A relation may carry `valid_from`, `valid_until`, `recorded_at` and
  `superseded_by`; the superseded statement stays readable in the file rather
  than being overwritten. `search`, `get_entry`, `relations` and `inventory`
  accept `--as-of` to ask what held on a given date, and `validate` reports
  overlapping intervals, cycles in a supersession chain, and a replacement
  dated before the statement it replaces. Validity sits on the relation
  because that is where contradictions accumulate and where an optional key
  costs nothing for the entries that do not need it -- the alternatives are
  weighed in `docs/adr/0006-relation-level-valid-time.md`;

- schema 10 records where a stored value came from, per field. The bookmark
  fetcher already reported `untrusted_web_metadata: true` for its reply, but
  that statement was dropped when the bookmark was written: a fetched title,
  description, author list and summary ended up in the vault with nothing
  distinguishing them from what the user had typed, and the only separation
  left in the file was a Markdown heading -- configurable prose that was
  German until 0.10.0a1. An entry may now carry an optional `provenance`
  object next to `attributes`, mapping field names to `web`, `user`, or
  `agent`, and body text taken from a page is wrapped in an
  `<!-- noetrail:web-content -->` fence. `get_entry` returns the field map
  together with the body's sections, their origin and their line spans;
  `search` returns a compact `web_fields` / `web_body` summary. Overwriting a
  field with `update` drops that field's mark, so a `web` label never ends up
  on the user's own words. This is information for the client's trust
  decision, not a control: it sanitizes nothing and enforces nothing;
- migration `9->10:add-field-provenance` marks the free-text fields of
  bookmarks whose `fetch_status` says a fetch produced them. It cannot know
  which of those a human later corrected, so it over-marks rather than
  under-marks. No field value and no body text is changed. Schema 8 and
  schema 9 remain readable and editable.

### Performance

- every command builds one vault index from a single scan and shares it.
  `find_entry`, `duplicate_titles`, `duplicate_bookmarks`, `duplicate_recipes`
  and `validate_relation_objects` each used to load the whole vault for
  themselves, and a `find_entry` miss re-walked it a fourth time only to count
  damaged files. Measured with `tools/measure_search.py` at 10 000 entries
  (median of 3, in-process, so the number is scan cost rather than interpreter
  startup, against a bare scan of ~0.5 s in the same runs): `capture`
  979 ms -> 536 ms, one `relate --add` plus one `relate --remove`
  1960 ms -> 1029 ms. `tag` already used a single scan and is unchanged at
  ~0.5 s. Nothing is persisted; the index lives for one command and Markdown
  stays the only source of truth;
- `search` computes the `sha256:` revision after the result page is sliced
  instead of for every match. A broad query over 10 000 entries used to read
  and hash all 10 000 files a second time to print at most 20 rows. Measured
  as the cost on top of the bare scan in the same run: 255 ms -> 172 ms. The
  saving is smaller than a second full read suggests, because those files are
  still in the page cache;
- `SchemaRegistry.custom_types` and `builtin_types` are cached instead of
  rebuilt on every access. They are read inside the search loop and twice per
  validated entry.

### Security

- the MCP server placed agent-supplied values directly into the CLI argument
  vector, so a value that looked like an option was parsed as one. A tag named
  `--remove` turned `set_tags(action="add")` into a deletion, and a literal
  `--` pushed the trailing `--expected-revision` into the positional list,
  silently disabling the optimistic-locking check. Every agent-controlled
  positional now follows an explicit `--` separator;
- a deeply nested JSON request raised `RecursionError`, which neither server
  caught: the stdio transport exited and left the session without a server,
  and the HTTP transport logged a full traceback. Both now reject over-nested
  documents before parsing them, through the new `noetrail.jsonrpc` module.

- the bookmark fetcher validated a destination by hostname and then let
  `urllib` resolve it again when it opened the socket. A DNS answer that
  changed between the two lookups was followed, which made an internal page
  reachable and its metadata retrievable. The validated address is now pinned
  for the connection, while the `Host` header, SNI and certificate check keep
  using the hostname;
- `64:ff9b::/96` (NAT64), `64:ff9b:1::/48`, `2002::/16` (6to4) and
  `224.0.0.0/4` are rejected. `ipaddress` reports them as global, so
  `64:ff9b::a9fe:a9fe` reached `169.254.169.254` on any cluster with NAT64.
  The same prefixes were added to the example network policy;
- a malformed entry no longer takes a whole command down with it. A
  non-hashable `type`, a `tags` value that is not a list, a `relations` value
  that is not a list, and a non-hashable relation target each raised
  `TypeError` -- including in `validate`, the one command meant to report
  them.

### Changed

- `search` and `retrieve` default to `--rank hybrid`, which runs the literal
  substring predicate and the BM25 ranking and returns the union, ordered by
  relevance with a literal-only match below every ranked one. On the
  repository's own retrieval set the default moves from Recall@3 0.300 to
  1.000: substring alone returned nothing at all for 14 of 20 queries, and the
  two queries BM25 misses are the infix and singular cases substring exists
  for. That gap reached users as "you have none" for entries that existed.
  Nothing previously findable is lost, because the substring candidate set is
  contained in the hybrid one, and `--rank substring` and `--rank bm25` remain
  available. A hybrid query pays both scans; `--sort auto` now resolves to
  relevance for any non-empty query. See
  [ADR 0007](docs/adr/0007-hybrid-retrieval-default.md). A saved view that
  names no `rank` follows the same default; it used to carry its own copy of
  the literal one, which is how the two would have drifted apart;
- the bookmark fetcher identifies itself to a site as
  `NoetrailBookmarkFetcher/0.2` instead of `PersonalKnowledgeBookmarkFetcher`.
  The rename to Noetrail never reached the one string this project sends to
  someone else's server, so every operator whose logs or robots rules named
  the fetcher saw the working title of a project that no longer goes by it;
- the rename is carried through the rest of the repository: the server is
  called the Noetrail MCP everywhere rather than the Knowledge MCP,
  `KnowledgeLayout` and `KnowledgeServer` are `NoetrailLayout` and
  `NoetrailServer`, the example deployment root is `/srv/noetrail`, and four
  test modules lost the old project name from their filenames. What ADR 0002
  keeps deliberately is unchanged: the `.knowledge/` format directory, the
  `knowledge*` console aliases, the `KNOWLEDGE_*` variables, the ZeroClaw
  server key with its `knowledge__*` tools, and the legacy distribution name.
  `RenameTest` fails on the specific forms that were left behind, so they
  cannot return a sentence at a time;
- entry frontmatter is read in the wider YAML subset ordinary Markdown tools
  produce, not only in the compact dialect Noetrail writes. Indented and
  parent-column block sequences, nested block mappings, unquoted and
  single-quoted scalars, comments, block scalars, and a `...` document end are
  all read correctly and normalized back to the canonical single-line form on
  the next write. An entry rewritten in an external editor therefore stays
  readable and editable, which matters more now that `import obsidian` exists:
  the importer hands a user a vault they are likely to open in Obsidian, whose
  property editor writes exactly the block style the reader used to reject.
  Constructs whose meaning is not local to the value -- anchors, aliases,
  tags, merge keys, complex keys, multi-line plain scalars -- are refused with
  a line number instead of guessed at, the same set `import obsidian` already
  refused in a foreign vault. Entries in the canonical dialect keep a fast
  path, so an unindexed scan is unchanged: 43.3 ms against a 45.2 ms baseline
  at 1,000 entries;

- `migrate` previews by default and writes only with `--apply`, matching
  `import` and `purge`. `--dry-run` is still accepted and is now a no-op;
- `tag` no longer case-folds the tags it stores. Folding decided both whether
  two tags were the same tag *and* how a tag was written, so adding one tag
  rewrote every other tag on the entry -- irreversibly for scripts where
  `casefold` is lossy, such as `Straße` becoming `strasse`. Comparison stays
  case-insensitive; the stored spelling is the author's;
- `migrate` validates each migrated entry before writing anything and refuses
  the whole run if one would not validate, applies the type's declared field
  defaults during the 8-to-9 move, and takes a copy of every entry it is about
  to rewrite when `--backup-dir` is given;
- `search` reports `complete` and `unreadable_entry_count`, as `inventory`
  already did. An entry that could not be read was indistinguishable from no
  match;
- `review` skips entries on an older schema and lists them under
  `skipped_legacy` instead of failing outright. One legacy entry used to make
  the whole queue unreadable;
- the trash listing and the review queue order by instant rather than by
  string. `now_iso()` writes local time with an offset, so the previous sort
  put entries written either side of a timezone change in the wrong order;
- duplicate frontmatter keys are rejected instead of silently taking the last
  value, which is what the schema-pack parser has always done;
- an entry body keeps its exact indentation across a write. The body was
  stripped on read and on write, so an entry opening with an indented code
  block lost that block on the next write of any kind;
- entries that cannot be read are skipped by every vault walk rather than only
  by some of them. `search`, `relate`, `relations` and `review` used to raise
  a bare `PermissionError` on a file with lost permissions.

### Internal

- the saved-view parser is held to the 95% coverage floor the other
  trust-boundary modules carry, and reaches 100%. Its refusals are the entire
  guarantee that a view cannot name a shell command, a path, a URL, or an
  unbounded value, and two thirds of them had never run in a test. Several
  accepting paths had not run either: `attribute_filters`, `related_id`,
  `relation_predicate`, `experienced`, `min_rating`, `domain`, and `as_of`
  could be declared in a view and silently not applied;

### Packaging

- reproducibility checks no longer mistake a local `build/` directory for
  an installed build frontend; the dependency-free backend fallback works
  after a source or wheel build too;

- installed-wheel acceptance now exercises the stdio MCP handshake and real
  search, retrieve and full-entry calls in both read modes, with and without
  the index. It checks aliases, tentative word-form labels, explicit body
  truncation, absent results and the wrong-type diagnostic after backup restore;

- a `Documentation` project URL pointing at `docs/`. The package page renders
  `README.md`, whose links are relative to the repository and therefore do not
  resolve there, so a reader arriving from a package index had no working way
  into the documentation;
- `README.md` now addresses every repository file by absolute URL through
  reference-style definitions, so the links work and both images render on a
  package index as well as on the repository. Reference labels keep the prose
  wrapped as before instead of burying it under inline URLs, and a test maps
  each URL back to a file that has to exist. The `uvx` blocks are opted out of
  the documentation runner because they install over the network, which is why
  they were unrunnable before the first upload and stay unrunnable after it;
- [Release process](docs/releasing.md) lists the sentences across four pages
  that stop being true when the repository becomes public or the package is
  uploaded, grouped by which of the two switches they depend on, with a test
  that fails once a quoted phrase no longer appears in its page;
- `tools/check_release_consistency.py` makes version, source fallback, current
  schema, changelog, release-notes filename/heading, and a release tag one
  checked chain. The release workflow runs it before building an artifact;
- the GitHub `pypi` environment now accepts deployments only from `v*` tags.
  Its required-reviewer rule remains an explicit publication blocker because
  GitHub rejects that rule on the current private-repository billing plan;
- `make publication-check` scans the current tree and every reachable Git blob
  for credential patterns, private-data paths, oversized unaudited blobs, and
  the retired private operational journal without printing values, object IDs,
  or affected filenames. The published decision record now contains product
  choices only; a history rewrite or clean public repository remains an
  explicit maintainer decision;
- the source distribution is usable: it contains `tests/`, `docs/`, `tools/`,
  `skills/`, `demo/`, `deploy/` and the project files, and CI builds it,
  unpacks it and runs the full suite from it. Previously `tests/__init__.py`
  was missing, so not a single test ran from an unpacked sdist;
- the release workflow publishes a source distribution alongside the wheel,
  and its tag filter matches final, `rc`, `a` and `b` tags. It previously
  matched alpha tags only, so `v1.0.0` would have produced no release build;
- every GitHub Action is pinned to a commit SHA, Dependabot watches them, and
  CodeQL runs on the Python sources;
- `mypy` and a dependency-free coverage measurement are CI gates. Coverage is
  held at 80 percent overall and 95 percent for `migrations.py`, `layout.py`,
  `jsonrpc.py` and the fetcher's SSRF path.
- the distribution installs one `noetrail` package instead of seven generic
  top-level modules. Import `noetrail.cli`, `noetrail.mcp`, and
  `noetrail.bookmark_fetcher`; console commands are unchanged;
- entry body section headings default to English (`## Personal note`,
  `## Generated summary`, `## Ingredients`, `## Preparation`,
  `## Personal notes`, `## Personal rating`) and are configurable through
  `body_headings` in `config.yaml`. Headings written by earlier development
  versions are still detected, so no content migration is required; set the
  overrides to keep writing the previous German headings in an existing vault;
- all documentation is English. The ZeroClaw guides moved to
  `docs/integrations/`, and generic MCP client setup is documented in
  [Connecting an MCP client](docs/integrations/mcp-clients.md);
- the Noetrail MCP server answers `inventory`, `search`, `get_entry`,
  `review_queue`, `relations`, and `list_trash` in its own process instead of
  starting a CLI process per call. A six-call agent turn drops from 316 ms to
  32 ms on a 100-entry vault and from 494 ms to 199 ms on a 1,000-entry vault.
  Mutations and `validate` still run in a separate process, and
  `noetrail-mcp --subprocess-reads` restores the previous behaviour;
- installed-wheel acceptance is a required CI gate after repeated successful
  runs, rather than an advisory job.

### Fixed

- publication history checks detect the operational journal under earlier
  filenames, private session links and credentials in commit messages;
  the release checklist includes GitHub pull-request refs and text revisions;

- release checksum manifests use artifact filenames, so verification works
  directly in the directory containing downloaded release files;

- the dependency-free coverage gate now measures the same executable source
  on Python 3.13 and 3.14. Python 3.14's deferred-annotation compiler creates
  uncalled `__annotate__` code objects whose line tables point at function
  signatures; those compiler-generated objects and multiline signature-only
  lines are excluded without lowering any coverage threshold;
- a photo delivered twice by a channel cost the attachment entirely.
  `list_pending_attachments` listed each file separately, so one image arriving
  as two byte-identical files looked like two candidates for one message; the
  caller is told to refuse an ambiguous mapping and did, and the user was told
  the photo could not be identified. There was never a choice to make, because
  blobs are stored under their digest and either copy produces the same
  attachment. Identical files are now listed once with `sha256` and
  `duplicate_count`, and the runtime instructions state that identical content
  is not ambiguity;

- `recipe` ran under a shared instead of an exclusive vault lock, so a recipe
  write could race with another writer. The lock mode is now declared per
  subcommand and covered by a regression test, rather than kept in a
  hand-maintained list;
- trash and restore relocate an entry with a single rename, so an interrupted
  move can no longer leave the same entry ID at two paths;
- an entry lookup no longer reports a plain "does not exist" when the vault
  contains unparseable files; it points at `noetrail validate` instead;
- an explicit `--know-script` override now handles reads as well as mutations
  instead of being bypassed by the in-process optimization;
- the in-process read path treats `SystemExit(None)` as the successful exit it
  represents in a separate Python process;
- installed-wheel namespace acceptance now runs from a neutral directory, so
  source-tree compatibility modules cannot be mistaken for wheel contents, and
  the local release check removes only known generated build directories before
  building so stale modules cannot leak into a wheel.

### Added earlier in this pre-release

- the public project name Noetrail, canonical `noetrail*` console commands,
  package metadata, and installed resource paths, with compatibility aliases
  for existing `knowledge*` deployments;
- separated application, built-in, instance-config, and private-data roots;
- declarative data-only schema packs and a complete generic custom-type
  lifecycle through CLI and MCP;
- bundled registry definitions for built-in types and schema-9 `attributes`;
- backward-compatible schema-8 reads/edits and a descriptive 8-to-9 migration;
- installable console commands, `noetrail init`, and `noetrail doctor`;
- synthetic multi-version CI and public contribution/security foundations;
- a safe generic Markdown importer with dry-run, stable provenance,
  idempotence, conflict reporting, and all-or-nothing writes;
- a fully synthetic demo vault and custom schema pack, five-minute quickstart,
  documented scaling limits, and installed-wheel upgrade/restore acceptance.
- complete read-only CLI/MCP inventory aggregates without entry titles, bodies,
  or the bounded search-result cap;
- compact, stably sorted CLI/MCP search pages with explicit totals and
  continuation offsets;
- compatibility shims at `tools/know.py`, `tools/knowledge_mcp.py`, and
  `tools/bookmark_fetch_mcp.py` for deployments that invoke those paths
  directly; they will be removed in 0.11.0;
- `ruff` linting, an installed-wheel acceptance job in CI, and a check that the
  distribution never reintroduces generic top-level module names;
- concurrency tests covering parallel captures and racing revision-checked
  updates;
- `tools/measure_search.py`, measured search and agent-turn latency in
  [Limits and scaling](docs/limits.md), and the recorded decision to defer a
  full-text index until a vault approaches 5,000 entries.

### Security foundations from earlier in this pre-release

- narrow Noetrail MCP and isolated bookmark-fetch capability boundaries;
- revision-checked mutations, shared PVC locking, safe trash, bounded image
  ingestion, secret scanning, and Git/private-data boundary checks.

### Governance and supply chain

- PyPI Trusted Publishing in `.github/workflows/release.yml`: TestPyPI first,
  then PyPI, each from a protected environment. `id-token: write` is granted
  only to the jobs that mint an OIDC token; the job that builds the code has
  `contents: read` and nothing else, and no API token exists for this project;
- signed build-provenance attestations for the wheel and the source
  distribution, verifiable with
  `gh attestation verify <file> --repo patsch1/noetrail`;
- `tools/generate_sbom.py`, which derives a CycloneDX 1.6 bill of materials
  from `pyproject.toml`. Its component list is read from
  `project.dependencies`, so the "no runtime dependencies" claim is checked
  rather than asserted. The document is a release asset;
- `SOURCE_DATE_EPOCH` pinned to the tagged commit's date, plus
  `tools/check_reproducible.py`, which rebuilds the distribution and compares.
  The wheel is byte-for-byte reproducible; setuptools' `sdist` ignores
  `SOURCE_DATE_EPOCH`, so the source distribution is compared by content and
  the timestamp-only differences are reported;
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `.github/CODEOWNERS`,
  `.github/PULL_REQUEST_TEMPLATE.md`, and three issue forms that require the
  program version, the Python version, the operating system, and a
  `noetrail doctor` report. Blank issues are disabled;
- `ROADMAP.md`: what comes after `0.10` and what is deliberately out of scope;
- `docs/adr/0003-in-process-reads.md` and
  `docs/adr/0004-no-search-index.md`, recording two decisions that were only
  described in a progress log before;
- a recorded demo. `demo/session.sh` runs one agent turn against a disposable
  copy of the synthetic vault; `tools/render_terminal_svg.py` executes it and
  writes `docs/assets/demo.txt` and `docs/assets/demo.svg`, which the README
  embeds. `tests/test_demo_recording.py` re-runs the session and fails if the
  recording no longer matches;
- README badges for CI, license, supported interpreters, the enforced coverage
  floor, and the runtime dependency count. No package-index badge while nothing
  is published.

### Governance and supply chain, changed

- `SECURITY.md` replaces "no fixed response-time guarantee" with stated
  commitments: acknowledgement within 72 hours, first assessment within 10
  days, fix or mitigation within 90 days, and disclosure at 90 days at the
  latest. It names both reporting channels, lists supported versions, and
  describes how release artifacts can be verified;
- `AGENTS.md` is now guidance for a contributing agent -- the six gates, what
  not to touch, commit and pull-request expectations -- instead of an
  instruction to work through the maintainer's private roadmap;
- `docs/open-source-roadmap.md` is split. The forward-looking part is
  `ROADMAP.md`; the retrospective phase and decision log moved to
  `docs/internal/decision-log.md`;
- `SUPPORT.md` and `CONTRIBUTING.md` point at the forms and templates that now
  enforce what they used to only ask for.

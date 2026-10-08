# Limits and scaling

Noetrail is designed for personal and small-team-sized local vaults. Markdown
portability and deterministic validation take priority over hidden indexes or
database-specific scale.

## Complexity

- Search without an index is linear in the number and combined text size of
  entries. It parses every active Markdown entry for each query and keeps
  matching result metadata in memory. The `sha256:` revision is computed only
  for the entries in the returned page, not for every match.
- Search with a fresh BM25 index is linear in the number of files (one stat
  each, for the freshness check) plus the length of the query's posting lists,
  and parses only the entries that scored. See
  [Ranking modes](#ranking-modes).
- A command performs one vault scan, not one per check. `find_entry`, the
  duplicate checks, and relation-target validation share a single in-memory
  index built inside the vault lock and discarded when the command ends. It is
  a snapshot, not a persistent index: nothing is written, and Markdown stays
  the only source of truth.
- Inventory is linear in active entry count but returns only complete aggregate
  counts. It does not return titles or bodies and is not capped by the MCP
  search-result limit.
- Validation is linear in entry and attachment count. It checks every active
  and trashed entry, relation target, and referenced attachment blob.
- Incoming-relation discovery scans all entries because relations are stored
  with their source entry.
- Trash, migrations, and imports preflight their selected set before writes.
- The BM25 index is a derived cache and is never part of the source of truth.
  It is verified against the vault before every use, and any mismatch means a
  full scan instead. Deleting it is always safe.
- No semantic or vector index is part of the source of truth. Vectors can only
  reorder lexical candidates, they are supplied by the caller, and Noetrail
  computes none; see [Embeddings](embeddings.md).

There is no hard vault-size ceiling. As an operational starting point, treat
10,000 Markdown entries as the upper edge of the default unindexed profile and
50,000 as the edge of the indexed one. Benchmark representative searches and
full validation before exceeding either. Large bodies reduce both figures,
because an unindexed scan loads entry text and an index grows with the
vocabulary it holds.

## Measured search latency

Reproduce these numbers with `python3 tools/measure_search.py --entries N`. The
synthetic entries are short thoughts with a twelve-word body; real vaults with
longer bodies scan more slowly. Measured on an Apple M-series laptop with a
local SSD and CPython 3.13, median of five runs:

| Entries | Interpreter startup | In-process scan | Full CLI subprocess |
| ---: | ---: | ---: | ---: |
| 100 | 15 ms | 2 ms | 50 ms |
| 1,000 | 17 ms | 25 ms | 87 ms |
| 5,000 | 15 ms | 134 ms | 261 ms |

Two things follow from this. The linear scan costs roughly 27 µs per entry, so
it stays comfortably below a human-noticeable threshold up to a few thousand
entries. And a fixed overhead of about 47 ms per call — interpreter startup,
module import, and argument parsing — dominates every query below roughly 2,000
entries. An agent that issues several searches per turn pays that overhead
every time.

## Measured agent-turn latency

Because that overhead is paid per call, the Noetrail MCP server answers the
frequent bounded reads — `inventory`, `search`, `retrieve`, `get_entry`,
`list_views`, `run_view`, `review_queue`, `relations`, and `list_trash` —
inside its own process. Mutations and
`validate` still run in a separate CLI process, which gives them crash
isolation and a 30-second hard timeout.

In-process reads have a 30-second cooperative deadline. File enumeration,
entry scans and scoring loops check it and return `read_timeout`, releasing the
vault lock; the next request gets a fresh deadline. This is not process
isolation: an individual blocking filesystem call, parser or sort cannot be
interrupted at a checkpoint while it is still running. `--subprocess-reads`
remains the option for a hard process timeout on reads.

The same measurement script times one representative turn: an inventory, three
searches, and two entry reads, six calls in total.

| Entries | Separate CLI process per read | Read in the server process |
| ---: | ---: | ---: |
| 100 | 316 ms | 32 ms |
| 1,000 | 494 ms | 199 ms |
| 5,000 | 1,350 ms | 1,049 ms |

The saving is the expected constant: six calls times the fixed per-call cost,
roughly 300 ms per turn regardless of vault size. Below about 1,000 entries
that removes nearly the whole turn latency; at 5,000 entries the linear scan
has become dominant and the same 300 ms is only a fifth of the turn.

That measurement is what triggered the index described below:
[ADR 0004](adr/0004-no-search-index.md) named 5,000 entries and a one-second
turn as the condition for revisiting the decision, and both were reached.
[ADR 0005](adr/0005-lexical-bm25-index.md) records what replaced it.

If the in-process path ever misbehaves, or a read needs the 30-second timeout
back, `noetrail-mcp --subprocess-reads` restores the previous behaviour without
a release change.

## Ranking modes

`search` has two lexical predicates and a hybrid mode with an empty-result
word-form and title/alias typo fallback. They share one
definition of which fields are searched, so a field declared `searchable: true`
behaves identically under any of them, and every structured filter, the page
contract, and the sort options are the same.

| | `--rank substring` | `--rank bm25` | `--rank hybrid` (default) |
| --- | --- | --- | --- |
| matches | the query as a literal substring of the searched text | entries containing at least one query token | either; bounded word forms, then title/alias typos, only if earlier stages are empty |
| finds `extractio` in "extraction" | yes | no | yes |
| finds `ramen berlin` across a title and a tag | no | yes | yes |
| default order | newest update, or newest occurrence | BM25 relevance | BM25 relevance |
| uses the index | no | yes, when one exists and matches | for its ranked half |

Hybrid is the default because neither single mode is a superset of the other
and the difference is not small: see [measured retrieval
quality](#measured-retrieval-quality). It first
runs the two above and returns the union, ranked by BM25, with a match only
the literal scan found sorted below every ranked one — a substring can fall
inside a word that means something else, so it is worth returning and not
worth returning first.

If that union is empty after filtering, hybrid scans the same searchable
fields for bounded word forms. One spelling may extend another by `e`, `en`,
`es`, `n` or `s`, with at least five letters in the shorter word. A compound
may split into two parts of at least five letters each: both must be supported
by distinct tokens in the same entry, and at least one part must be a whole
token. Thus `Fahrradkette` can find "Fahrradwartung" plus "Kette", while
`Bernsteinkette` cannot match that entry through "Kette" alone.

These candidates carry `match_kind: word_form` in search and retrieve. They
are tentative, not semantic equivalents; read their content before answering.
Queries with more than 16 unique tokens do not use this fallback. Existing
nonempty results, filters, pagination and the two single modes are unchanged.
The fallback costs another tokenizing scan, even with a fresh index. It does
not change the BM25 vocabulary or add synonyms.
See [ADR 0008](adr/0008-word-form-fallback.md) for the tradeoff.

If the word-form stage is also empty, hybrid checks **title and alias tokens
only** for one insertion, deletion, substitution or adjacent transposition.
Both tokens must contain 5–64 letters; numeric and hash-like tokens are excluded.
It does not fuzzy-match bodies, tags or custom fields. Results carry
`match_kind: typo` and up to two observed `matched_terms`; opt-in explanations
show the actual spelling and its provenance, not an invented correction.
They are tentative candidates, not proof that two names identify the same thing.
This is another scan after an empty result. Nonempty lexical/word-form results
and explicit `substring`/`bm25` modes stay unchanged.

### Several query variants in one retrieval

`retrieve` accepts one query and up to three `--query-variant` options; MCP uses
`query_variants`. All four strings together share the existing limit of ten
entries and 100,000 body characters, the type restriction and `as_of` instant.
Each string is evaluated independently against the same read-locked vault.
The union is deduplicated by entry ID. Duplicate IDs in different candidate
files cause a refusal; they are not silently merged. Full candidate sets are
fused before the shared limit is applied, with reciprocal rank fusion (`k=60`)
and ID tie-breaking. Lexical hits precede word-form and typo-only candidates;
raw BM25 scores from different strings are not added together.

Batch strings must be nonblank, distinct after NFKC/case/edge-whitespace folding,
NUL-free and at most 2,000 characters each. Each returned entry's `query_matches`
records the matching strings and whether they yielded lexical, word-form or
typo evidence. Bodies, revisions, provenance and truncation indicators retain
their normal meaning. With no variants the single-query response stays unchanged,
including the existing empty-query listing behavior. Noetrail does not generate
translations or synonyms; the caller supplies them. See
[ADR 0009](adr/0009-typo-and-query-variants.md).

## The derived BM25 index

`noetrail index rebuild` builds an inverted index below the *instance config
root* — never below the data root, so a backup of the vault cannot contain it
and a restore cannot resurrect it. It is opt-in: with no index file, nothing
about writing changes.

Every read verifies it before using it, by comparing a digest over
`(vault-relative path, mtime_ns, size)` for every Markdown file and a
fingerprint of which schema fields are declared searchable. Any mismatch, a
failed payload checksum, a truncated file, or an index built for another data
root all end in the same place: the index is not consulted and the answer comes
from a full scan instead. The two paths are required to return byte-identical
responses.

The known blind spot is the one every modification-time cache has: a rewrite
that keeps both the timestamp and the byte length is invisible to the cheap
check. `noetrail index status --verify` re-derives the index from the Markdown
and compares content, which catches it and costs as much as a rebuild.
`noetrail doctor` reports the state on every run, always as a warning and never
as an error, because a stale cache costs latency and not correctness.

Once an index exists, it is refreshed incrementally after every successful
mutating command, inside the exclusive vault lock that command already holds.
`noetrail index drop` removes it.

## Measured BM25 and index cost

Reproduce with:

<!-- docs-check: skip - takes several minutes and writes 50,000 files -->
```sh
python3 tools/measure_search.py --entries N --repeats 3 \
  --vocabulary 20000 --body-words 60 --skip-agent-turns
```

Measured on a two-core Linux container with local disk and CPython 3.11,
median of three runs. These are **not** the same machine as the tables above,
which were taken on an Apple M-series laptop; compare the columns within a
table, not across the two sets.

The synthetic corpus here is deliberately richer than the one those earlier
tables use: sixty-word bodies drawn from a twenty-thousand word vocabulary.
The default fifteen-word vocabulary keeps the older numbers reproducible but
says nothing useful about an index, because every query matches nearly the
whole vault.

Query latency, in-process, for one page of results:

| Entries | `--rank substring` | `--rank bm25`, fresh index | `--rank bm25`, no index |
| ---: | ---: | ---: | ---: |
| 1,000 | 62 ms | 21 ms | 123 ms |
| 10,000 | 608 ms | 140 ms | 1,248 ms |
| 50,000 | 3,279 ms | 718 ms | 7,542 ms |

Two things to read from that. With a fresh index, ranking is 2.9x to 4.6x
faster than the substring scan it replaces, because only the entries that
scored are read. Without one, `--rank bm25` is about 2x *slower* than
substring, because it tokenizes the whole vault for every query — which is the
price of the guarantee that deleting the index never changes an answer.

For a nonempty lexical result, `--rank hybrid` runs both routes and costs their sum: with a fresh index,
roughly the substring column plus the indexed one; without an index, roughly
the substring column plus the unindexed one. That is the price of the recall
below, and it is the argument for building the index on a vault large enough
for the numbers above to matter. A vault of a few thousand entries pays
milliseconds; `--rank substring` remains available for anyone who would rather
have the cheapest possible scan than the answer.

What the index costs:

| Entries | full build | on disk | per entry | freshness check | incremental update, one entry |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.22 s | 1.4 MB | 1,359 B | 9 ms | 95 ms |
| 10,000 | 2.3 s | 9.4 MB | 943 B | 97 ms | 485 ms |
| 50,000 | 11.7 s | 45.2 MB | 904 B | 505 ms | 2.4 s |

The freshness check is one stat per file and runs on every indexed query, so it
is the floor under the latency above: at 50,000 entries it is 505 ms of the
718 ms.

The incremental update re-reads only the entries whose signature moved, but
rewrites the whole index file, and that rewrite is what the column measures.
It is charged to every write once an index exists:

| Entries | `capture` without an index | `capture` with an index |
| ---: | ---: | ---: |
| 1,000 | 61 ms | 145 ms |
| 10,000 | 561 ms | 1,070 ms |
| 50,000 | 3.3 s | 6.4 s |

At 50,000 entries the index roughly doubles the cost of a capture. That is the
honest reason it is opt-in rather than automatic, and the reason a
write-heavy vault of that size should measure before switching it on.

## Measured retrieval quality

**This is a self-made set, not a benchmark.** The corpus, the queries, and the
relevance judgements in `tools/retrieval_set.json` were all written in this
repository. The number below says which of the two ranking modes finds the
entries its own author meant, and nothing about how Noetrail compares to any
other system. LoCoMo, LongMemEval and BEAM cannot be fetched or run in this
build environment and no result from them is claimed.

Reproduce with:

<!-- docs-check: skip - builds a temporary vault of 34 entries -->
```sh
python3 tools/measure_retrieval.py --markdown
```

34 entries, 20 queries, one page of ten results:

| Mode | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| `substring` | 0.275 | 0.300 | 0.300 | 0.300 | 0.300 |
| `bm25` | 0.825 | 0.900 | 0.900 | 0.900 | 0.900 |
| `hybrid` | 0.925 | 1.000 | 1.000 | 1.000 | 1.000 |

Substring matching returns *nothing at all* for 14 of the 20 queries. That is
not a defect in the implementation; it is what matching a literal substring
means once a query has more than one word, and it is the finding that
justified the index far more than the latency did.

BM25 misses exactly two queries, both kept in the set on purpose: `hydrat`, a
prefix of "hydration", and `noodle`, where the text says "noodles". Those are
the cases a tokenised ranking cannot answer and a substring scan can.

Those two facts are the whole argument for the default. Each mode fails where
the other succeeds, so the union fails nowhere on this set, and it is better
than either at rank 1 as well: the two literal-only answers arrive rather than
being replaced by a worse ranked one. Read the 1.000 as "no query in a set
this small survived both routes", not as a claim about retrieval in general —
20 queries cannot establish a ceiling, and this set was written here. See
[ADR 0007](adr/0007-hybrid-retrieval-default.md).

## Enforced per-operation limits

| Surface | Limit |
| --- | ---: |
| one imported Markdown file | 2 MiB |
| Markdown candidates in one import run | 10,000 |
| one image attachment | 20 MiB |
| attachments referenced by one entry | 128 |
| custom array field | pack-defined, at most the declared `max_items` |
| recipe preparation or cooking duration | 10,080 minutes |
| one CLI or Noetrail MCP search page | 50 entries |
| entries returned by one `retrieve` call | 10 |
| body text returned by one `retrieve` call | 100,000 characters |
| query strings in one `retrieve` call | 4 (original plus up to 3 variants) |
| aliases on one entry | 64 |
| one alias | 200 characters |

Use `noetrail inventory` or the canonical MCP tool `inventory` for a complete high-level
overview regardless of entry count. A search result is a bounded selection,
not proof that no additional matches exist. Search defaults to 20 entries and
returns `total`, `returned`, `has_more`, and `next_offset`; pass the latter as
`offset` with the same query, filters, and sort order to continue. Results are
stably ordered by newest update by default, or by newest occurrence for
experience queries. Explicit title, update, and occurrence ordering is also
available.

Each result contains compact metadata, a revision for safe follow-up changes,
and relation/attachment counts rather than paths, bodies, or full relation
arrays. Use `retrieve` when one answer needs several matching bodies in one
bounded call, and inspect its explicit truncation fields. Use `get_entry` for
the complete body or full metadata of one selected entry.

The Markdown importer can use `--limit N` for a deterministic trial. Unlike
search pagination, this is a selection limit, not pagination state; omit it
for the reviewed full run.

## Deployment considerations

- Keep the data root on local or low-latency persistent storage. Network
  filesystems must preserve atomic rename and advisory file-lock behavior.
- Back up attachments and Markdown in the same recoverable data-root boundary.
- Keep fetched web content outside the vault-facing process.
- MCP and agent timeouts are deployment choices; they may need adjustment for
  a large vault even when the underlying CLI operation is healthy.
- The BM25 index lives under the instance config root, not the data root, so a
  backup of the data root never contains it and a restore never resurrects it.
  Back up the data root; rebuild the index with one command if you want it
  back.

For workloads beyond the default profile, record entry count, total Markdown
bytes, attachment count, storage type, Python version, and timings for search
and validation before choosing an optimization.

## Review follow-up measurements

The measurement script now times explicit substring, indexed/unindexed BM25,
and indexed/unindexed hybrid separately, including a multiword query. The
agent-turn sample uses three multiword searches. `--skip-agent-turns` skips
execution, not just the reported fields.

Measured on macOS arm64, Python 3.14, local disk, median of three runs:
`python3 tools/measure_search.py --entries 1000 --repeats 3 --vocabulary 20000
--body-words 300`. These longer synthetic notes test more text per entry than
the earlier table; compare within this table, not between different machines.

| Operation | Median |
| --- | ---: |
| substring, `obsidian lantern` | 64.0 ms |
| BM25 indexed, same query | 19.4 ms |
| hybrid indexed, same query | 75.8 ms |
| BM25 scan, same query | 230.4 ms |
| hybrid scan, same query | 245.7 ms |
| six-call agent turn, in-process, no index | 989.0 ms |
| six-call agent turn, subprocess, no index | 1560.4 ms |
| capture without / with index | 65.8 / 145.8 ms |
| one-entry index maintenance | 82.9 ms |

The extra literal pass is a small fraction of unindexed hybrid cost in this
sample. Keep the existing optional index and prioritize retrieval quality;
this 1,000-entry run does not justify a new storage engine or extrapolation to
50,000 entries. Whole-index rewrites remain a measured scaling limitation.

Nonempty queries with no tokens (emoji, punctuation or tokens over 64 characters)
never mean "list everything": hybrid applies the literal query, BM25 returns
no candidates. Only the empty string requests an unfiltered text listing.
See [everyday diagnostics and search evidence](retrieval-evaluation.md) for the
100-question suite and bounded match explanations.

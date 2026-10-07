# ADR 0005: A derived BM25 index, and no shipped embedding provider

- Status: accepted
- Date: 2026-08-07
- Supersedes: [ADR 0004](0004-no-search-index.md)
- Roadmap phase: 13

## Context

[ADR 0004](0004-no-search-index.md) refused an index and wrote down the
condition under which the refusal expires: re-measure when the vault
approaches 5,000 entries, or when a representative turn exceeds one second.
Both have been reached, and a second thing has changed that 0004 did not
consider at all.

**The latency trigger fired.** Re-measured with `tools/measure_search.py` on
the corpus described in [Limits and scaling](../limits.md), a search over
10,000 entries costs about 610 ms and over 50,000 entries about 3.4 s. That is
past the threshold 0004 named.

**The quality problem was never about latency.** Search matches the query as a
literal substring of the entry's searched text. A user who types
`espresso extraction bitter` gets nothing unless those three words appear
adjacently in that order, and `ramen berlin` finds nothing even when the vault
holds a note titled "Kreuzberg ramen place" tagged `berlin`. On the retrieval
set in `tools/retrieval_set.json` — written for this repository, not an
external benchmark — substring matching answers with Recall@10 0.300 and MRR
0.300, against 0.900 and 0.900 for BM25. Making the scan faster would not have
moved either number. This is the reason 0004 is superseded rather than merely
re-triggered: it framed the question as speed, and the larger defect was that
a multi-word query returns nothing.

The constraints 0004 put on any future index have not weakened. Markdown stays
authoritative; the index must be fully rebuildable, safely deletable, never
required for a restore, and never a second place where a user-visible value is
stored. An entry missing from the index is a stale index, not a missing entry.

## Decision

**Build a lexical BM25 index as an opt-in, derived cache.** Okapi BM25 with the
Lucene IDF variant, `k1` and `b` adjustable per query with defaults 1.2 and
0.75. Tokenisation is Unicode-aware — NFKC plus `str.casefold`, letters, digits
and marks in any script — and has no stemmer, no stop list, and no synonyms,
because a stemmer that is right for English is wrong for German compounds and a
vault is written in whatever its owner writes in. Implementation is standard
library only; no dependency is added.

**Substring stays the default.** `--rank bm25` opts in. Substring matching does
one thing tokenised ranking cannot: it matches a prefix or an infix of a longer
word, so `extractio` finds "extraction" and `hydrat` finds "hydration". The
retrieval set keeps two such queries precisely so the trade is visible. Every
structured filter, the page contract, and the sort options are unchanged under
either mode; only the text predicate and the default order differ.

**The index is a cache with an enforced freshness check.** It lives under the
instance config root, never under the data root, so a backup of the data root
cannot contain it and a restore cannot resurrect it. Every read verifies it
against the vault first — a digest over `(vault-relative path, mtime_ns, size)`
for every Markdown file, plus a fingerprint of which schema fields are declared
searchable — and any mismatch means the index is not consulted at all. The
answer then comes from the same BM25 code driven by a full scan. The two paths
are required to be identical, not merely similar:
`tests/test_search_index.py` compares whole JSON responses across eleven
queries with the index present and absent, and `noetrail.search.score_query`
accumulates in a fixed term order in both so the floating-point scores are
bit-identical rather than close.

`(path, mtime_ns, size)` rather than the existing `sha256:` revision chain,
because the revision is computed by reading the whole file: verifying freshness
that way costs exactly what the scan the index replaces costs. The stat walk is
measured at 102 ms over 10,000 entries against 494 ms for the scan. The price is
the known blind spot of every mtime-based cache — a rewrite that keeps both the
timestamp and the byte length is invisible — so `noetrail index status --verify`
re-derives the index from the Markdown and compares content, and `doctor`
reports the state on every run.

**Damage is discarded, never partially believed.** The file is one JSON header
line over fixed-width binary sections, written to a temporary name, fsynced and
renamed. The header carries a SHA-256 of the payload that is verified before a
single posting is read, so a truncated, extended, or edited file is rejected and
search falls back to the scan.

**Maintenance is opt-in and lives in the dispatcher.** No index file means no
index work, so a user who never runs `noetrail index rebuild` pays nothing.
When one exists, `noetrail.cli.run_command` refreshes it incrementally after a
successful mutating command, inside the exclusive vault lock that command
already holds. That is one place rather than fourteen, so a new writing command
cannot forget it, and it keeps the rule in `tests/test_locking.py` intact:
no handler reads the shared `VaultIndex` snapshot after writing, and the three
`index` subcommands are three separate handlers so that `status` can stay a
shared-lock read while `rebuild` and `drop` declare the exclusive lock.

**No embedding provider is shipped, and an interface is.** See
[Embeddings](../embeddings.md). Noetrail defines a vector sidecar file format
and can reorder BM25's candidates by cosine similarity against a query vector
the caller supplies; it computes no embeddings, ships no model, and opens no
socket for one.

## Consequences

- A multi-word query works. The measured retrieval numbers are in
  [Limits and scaling](../limits.md) alongside the caveat that the set is
  self-made.
- Searching with a fresh index is 2.9x faster at 1,000 entries and 4.2x at
  10,000 than the substring scan, because only the entries that scored are
  read. Without an index, `--rank bm25` is about 2.5x *slower* than substring,
  since it tokenises the whole vault per query. Both numbers are published.
- The index costs disk — roughly 0.9 to 1.4 kB per entry on the measured
  corpus — and costs time on every write once it exists, because the whole
  file is rewritten: about 93 ms at 1,000 entries and 464 ms at 10,000 on top
  of the write itself. That is the honest reason it is opt-in rather than
  automatic, and the reason the practical ceiling in
  [Limits and scaling](../limits.md) is stated per profile.
- There is now a derived artefact that can be wrong. Everything above exists so
  that "wrong" resolves to "slower", never to "a different answer". Deleting
  the index is always safe and always correct.
- Restore stays a file operation over the data root. Nothing in a backup refers
  to the index.

## Alternatives considered

**Make BM25 the default.** Rejected. It would silently change the meaning of
every stored query, script, and agent prompt, and it loses infix matching,
which the retrieval set shows is a real capability and not a bug. The default
can be revisited once `--rank bm25` has been the documented recommendation for
a release.

**Store the index as JSON.** Rejected on measurement. At the fifty-thousand
entry shape, parsing the postings as JSON took about 0.64 s against 0.02 s for
the same data as `array` sections — which would have made the cache slower to
open than the fraction of the scan it saves. The header stays JSON so that
`head -1` still answers what was indexed, when, and against which vault.

**SQLite, including FTS5.** Rejected. It is in the standard library, but FTS5
is a build option rather than a guarantee, its ranking is its own, and the file
is not inspectable with an editor. The current format is a documented layout
this repository can read with `array.frombytes`; that is worth more than the
convenience.

**`marshal` for the postings.** Rejected although it is stdlib, fast, and
compact. `marshal` is documented as unsafe on malformed input, and a project
whose pack format is deliberately non-executable should not introduce a
deserializer that can crash the interpreter, checksum or no checksum.

**Tombstones with periodic compaction.** Rejected as unnecessary. Document
slots are reused on delete, so a single edit patches the affected posting lists
in place and never renumbers ordinals.

**A bundled embedding provider.** Rejected; see
[Embeddings](../embeddings.md) for the argument. In short it costs either a
large native dependency, or executable code in a pack, or the text of a private
vault going to a third party — and it would make a ranking depend on a model
version, so the same query returns different entries after an upgrade and no
reader can reproduce it from the files. That last objection is
[ADR 0004](0004-no-search-index.md)'s and it still stands; it is the one part
of 0004 this record keeps rather than replaces.

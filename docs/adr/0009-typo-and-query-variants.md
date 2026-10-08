# 0009: Labelled title typos and bounded query variants

Status: accepted. Date: 2026-10-08.

## Context

Synthetic client checks exposed two costs: a misspelled title can require a
second model turn, and a language change can stop at an irrelevant lexical hit.
The existing word-form fallback recovers inflections and grounded compound
splits but cannot recover an adjacent transposition. Repeating CLI/MCP searches
also repeats model round trips before synthesis.

## Decision

Extend empty hybrid retrieval with one-edit title/alias candidates only after
lexical and word-form candidates are empty. Restrict edits to whole alphabetic
tokens of 5–64 characters and exclude hash-like strings. Preserve explicit
lexical modes, structured filters, cache/scan parity and all nonempty earlier
results. Mark candidates `match_kind: typo`, retain observed spellings and
source provenance, and never persist generated aliases.

Allow `retrieve` callers to supply three additional query variants alongside
the original query. Each may be a correction, short wording or translation.
Noetrail generates none of them. Evaluate them under one shared read lock,
deduplicate valid entry IDs and fuse complete candidate sets before pagination.
Reciprocal rank fusion with `k=60` avoids adding incomparable BM25 scores.
Lexical candidates precede word-form and typo-only candidates; stable IDs
break ties. Report each candidate's matching queries and match kinds.

All variants share ten returned entries, 100,000 body characters, type filters
and the same `as_of` instant. Batches reject blank, equivalent, oversized or
NUL-containing strings. Duplicate IDs in different candidate files are an error.
The single-query wire shape remains unchanged when no variants are supplied.
The MCP server only builds CLI arguments; the vault rules remain in the CLI.
ZeroClaw counts every variant toward the existing four-query turn budget.

## Consequences and alternatives

Typos remain tentative, and an empty search still cannot prove absence. The
fallback costs another metadata scan, even with a fresh index. Batch search
can do up to four candidate evaluations; it reduces model round trips, not
necessarily engine CPU time. Full-body, revision and provenance rules remain.

General fuzzy matching of bodies would create too many weak candidate anchors.
A hand-written synonym/translation dictionary would tune retrieval to a tiny
dataset and particular languages. Bundled embeddings would add dependencies
and provider/privacy decisions. Neither is introduced. External vectors still
rerank lexical candidates rather than define knowledge truth.

There is no on-disk format change, migration, network call, runtime dependency,
new MCP path argument or change to mutation isolation/timeouts.

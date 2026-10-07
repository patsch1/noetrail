# ADR 0008: Word-form candidates after an empty hybrid search

- Status: accepted
- Date: 2026-09-20
- Supersedes: the no-additional-predicate part of [ADR 0007](0007-hybrid-retrieval-default.md)
- Proposal: [issue 67](https://github.com/patsch1/noetrail/issues/67)

## Context

Agent reformulation improves retrieval but can still miss ordinary spelling
forms and compounds. `Fahrradkette` has no lexical match in a note containing
"Fahrradwartung" and "Kette". Global stemming or unrestricted fragments would
also add candidates to already useful results and make false positives harder
to diagnose.

## Decision

Only an empty filtered hybrid union may invoke a separate word-form scan.
Accept the endings `e`, `en`, `es`, `n`, `s` when one observed spelling extends
the other and the shorter word has at least five letters. Accept a compound
split only when both parts have at least five letters, both are prefixes of
distinct observed tokens in the same entry, and at least one is a whole token.
All terms use the existing normalization. Ignore nonalphabetic terms and
disable this fallback above 16 unique query tokens.

Mark every fallback candidate with `match_kind: word_form`, including retrieve.
Use equal internal scores and the existing stable tie-breaker; do not present
a heuristic score as BM25 relevance. Explanations may quote the supporting
tokens, preserving their recorded provenance and existing excerpt bounds.
No default body excerpts are added. MCP descriptions require content checking.

## Consequences

The two lexical modes, existing nonempty hybrid results and cached/uncached
parity stay unchanged. Filtered-out lexical hits cannot suppress valid fallback
candidates inside the requested scope. Pagination never triggers the fallback
merely because a later page is empty.

This is a conservative spelling heuristic, not linguistic analysis or semantic
equivalence. It can miss short compounds, umlaut changes and synonyms, and can
still return unrelated words with similar spelling. Explicit labels and a
separate false-positive measurement matter more than claiming those cases are
solved. Diagnostics and their limits are in
[retrieval evaluation](../retrieval-evaluation.md).

Empty searches pay an additional tokenizing scan. The scan uses the same
deadline checks and searchable fields as other reads, and changes neither the
persisted cache format nor stored entries. Reverting the change needs no data
migration; callers can also explicitly select a lexical single mode.

## Alternatives rejected

Global stemming or fuzzy edit-distance matching would change successful
queries as well and need broader language-specific evidence. Matching a single
compound fragment would turn `Bernsteinkette` into a bicycle hit. Requiring
only two prefixes, without a whole-token anchor, would admit arbitrary split
accidents. Embeddings remain a separate decision with runtime and privacy costs.

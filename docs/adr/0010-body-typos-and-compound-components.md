# ADR 0010: Compound parts and body typos after every other stage

- Status: accepted
- Date: 2026-10-29
- Extends: [ADR 0008](0008-word-form-fallback.md) and
  [ADR 0009](0009-typo-and-query-variants.md)

## Context

On the 100-question diagnostic, hybrid finds 45 of 75 answerable questions.
Two of the misses are lexically reachable typos whose correct spelling is in
the body, not in the title: `Mondarchvi` for "Mondarchiv" and `Morgenstren`
for "Morgenstern". ADR 0009 excludes them on purpose: it allows "one-edit
title/alias candidates only after lexical and word-form candidates are
empty", because "general fuzzy matching of bodies would create too many weak
candidate anchors".

Another miss is a compound read from the other side. `Zugreise schlafen`
shares no token with a note titled "Nachtzugreise". ADR 0008's compound rule
covers the query holding the compound and the note holding its parts
(`Fahrradkette` against "Fahrradwartung" and "Kette"); it does not cover the
query holding a part and the note holding the compound. Its implementation
states that arbitrary infixes are insufficient evidence.

The remaining misses are paraphrases and translations with no shared spelling.
Following the roadmap's decision against bundled embeddings, those stay agent
work: the caller supplies query variants (ADR 0009).

## Decision

Add two stages after the existing ones. Each runs only if the filtered
lexical union, word forms, title/alias typos and every earlier new stage
returned nothing, in this order:

1. **Compound parts.** A query token of at least six letters is a candidate
   anchor if a searchable token of the same entry starts or ends with it and
   is at least three letters longer. Both are alphabetic and not hash-like.
   Candidates carry `match_kind: word_form`, score `-1.5` (below word forms,
   above typos) and up to two supporting compounds in `matched_terms`.
2. **Body typos.** The ADR 0009 one-edit rule, applied to body tokens. Both
   tokens need at least seven letters, must be alphabetic and not hash-like;
   at most two observed tokens are reported. Tags and custom fields are not
   consulted. Candidates carry `match_kind: typo`, score `-3.0` (below title
   typos) and the observed spellings in `matched_terms`.

This extends ADR 0009's "title/alias candidates only" to body words, and ADR
0008's compound evidence to a part found at either end of a compound.
Everything else those records decide stands: the 16-token cap, the labels, no
stored aliases or corrections, filters and `as_of` before any fallback, the
deadline checks, unchanged explicit lexical modes and identical answers with
and without the index. Explanations quote the observed token.

### Why these guards make the extension safe

- **Last place.** Neither stage can change any result an earlier stage
  produces. Nonempty lexical, word-form and title-typo answers are untouched.
  This is what separates the body stage from the "general fuzzy matching of
  bodies" ADR 0009 rejected: it never adds a weak anchor next to a real one.
- **The query word is absent.** Reaching these stages means no query token is
  a token of any entry in scope, because BM25 would otherwise have matched
  it. A correctly spelled word the vault contains therefore never triggers
  fuzzy matching. The guard is not extended to entries outside the filters:
  as in ADR 0008, content the caller excluded does not change in-scope
  results.
- **Length.** A body has far more words than a title, so more of them are one
  edit from some query word. On the 100-question set a five-letter minimum
  turned `kalte` into "Karte" and `nicht` into "Nacht"; six and seven gave
  the same results there, and seven is the conservative choice.
- **Anchored, long components.** A one-word query already finds a compound
  as a literal substring, infixes included. The component stage gives each
  token of a multi-word query the same chance, more strictly: the part must
  open or close the compound. A five-letter minimum let `Jacke` find
  "Regenjacke" for a zipper question; six excludes it. The three-letter
  extension leaves endings such as `-en` to the word-form rule.

## Consequences

On the locally authored 100-question set, hybrid Recall@5 rises from 0.600 to
0.653: `Mondarchvi`, `Morgenstren`, `Zugreise schlafen` and `ruhiger Ort zum
Kaffeetrinken` (body "ruhigen"). Correct-empty answers stay at 24/25 and
irrelevant results at 12. The word-form transfer set, the 20-query regression
set and both explicit lexical modes are unchanged. Details are in
[retrieval evaluation](../retrieval-evaluation.md).

The typo transfer set had labelled its body case `Dampfventli` (body
"Dampfventil kontrollieren.") as an absent distractor, because it was written
to assert ADR 0009's title/alias-only rule. Under this decision that note is
the correct, labelled, last-resort candidate, so the label was changed to
answerable with that note as the answer. The change follows the policy, not
the measurement. That set therefore no longer serves as transfer evidence for
body typos. The evidence is the four 100-set recoveries. The set's
numeric and hash-like aliases remain absent distractors and stay empty.

Body typos are a spelling heuristic. Two ordinary words one edit apart, such
as an inflected adjective and a noun, can produce an unrelated candidate; the
label, `matched_terms` and explanations are what let a caller notice that.
The small sets do not establish a general false-positive rate.

`Balkon` still does not find "Balkonbewässerung" in `Pflanzen auf dem Balkon
wässern`, because `auf` and `dem` already match other entries and the
fallbacks never run after a nonempty result. Paraphrases and translations
remain agent work.

An empty hybrid search can now pay up to four tokenizing scans. Each checks
the read deadline. Nothing is persisted: no index, cache format, stored entry
or wire shape changes, and `match_kind` keeps its two existing fallback
values. Reverting needs no migration.

## Alternatives considered

- **Only words capitalised inside a sentence**, as a stand-in for names. In
  German that means any noun not at a sentence start, so it is not a name
  test. It was also first chosen while the `Dampfventli` label was in view,
  and a rule shaped by a fixture is worse than a weaker measured number.
- **Any infix.** No measured difference, but a part inside a word is the
  weakest evidence (`eichen` in "Zeichenblock"), and ADR 0008 rejected it.
- **Requiring the query token to be absent from every entry, including
  filtered-out ones.** In scope it is already guaranteed; outside scope it
  would let excluded content change results.
- **Requiring the first letter to match**, as many fuzzy matchers do. It made
  no measured difference and would add another rule with no evidence for it.
- **A new `match_kind`.** Compound parts are word forms and body typos are
  typos; separate scores and `matched_terms` already tell them apart, so the
  MCP and CLI wire shapes stay unchanged.
- **Embeddings or a synonym list**, for the remaining paraphrases. Both stay
  rejected for the reasons in ADR 0009.

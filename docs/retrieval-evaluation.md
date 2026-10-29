# Everyday retrieval diagnostics

Run `python3 tools/measure_questions.py` for the 100-question diagnostic set.
It contains 21 synthetic entries, German paraphrases, language changes, typos,
exact names, eight structured temporal queries, and absent answers. Relevance
judgements are allowed to have no lexical overlap with their answers. This is
locally authored diagnostic data, not an independent or external benchmark.
Keep the older 20-query set as a regression check for the two lexical engines.

The report separates answerable recall/MRR at five from correct empty answers.
Precision is micro-averaged over the actual returned top-five results, with
irrelevant result counts and per-category/per-question details. Empty relevance
lists do not increase recall. A search hit is a candidate passage, not proof
that the requested fact is present; answer synthesis needs a separate agent
evaluation such as the [alpha pilot](alpha-pilot.md).

Initial result with the non-token query fix, Python 3.14 on macOS:

| Mode | Recall@5 (75 answerable) | Precision of returned top-five | MRR@5 | Correct empty (25 absent) |
| --- | ---: | ---: | ---: | ---: |
| substring | 0.307 | 1.000 | 0.307 | 1.000 |
| bm25 | 0.360 | 0.692 | 0.353 | 0.960 |
| hybrid | 0.373 | 0.700 | 0.367 | 0.960 |

Hybrid's exact-title and structured-time recall are 1.000, but paraphrase and
cross-language recall are both 0.111, and typo recall is 0.167. This measures
one query per question; it does **not** measure the new agent retry instructions.
Those instructions should be evaluated in real client sessions before claiming
an improvement. Do not tune synonyms to this tiny set or describe its score as
a general quality estimate.

After bounded agent reformulation, the next engine change adds word forms
only when the filtered hybrid union is empty ([ADR 0008](adr/0008-word-form-fallback.md)).
Compared with revision `297b0eb`, the same 100-question set now yields:

| Hybrid | Recall@5 | Precision | MRR@5 | Correct empty | Irrelevant results |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before fallback | 0.373 | 0.700 | 0.367 | 24/25 | 12 |
| With fallback | 0.427 | 0.727 | 0.420 | 24/25 | 12 |

Four more of the 75 answerable queries find their entry. Both single modes
and every metric of the older 20-query regression remain unchanged. These are
single-query engine results, not agent answer-quality scores.

Run `python3 tools/measure_questions.py --set tools/word_form_set.json` for
20 additional transfer cases. This set was authored after fixing the rules,
without retuning them, but by the same author: it is not an independent
benchmark. Eight of 12 answerable queries now return the relevant entry,
compared with zero before; all eight absent-answer distractors remain empty
and no irrelevant result is added. Four intentional limitations remain:
`Bäume`, `Bücher`, `Running` and the short compound `Segeltuch`. The small sets
do not establish a general false-positive rate. In particular, two supporting
tokens in a note do not prove the compound's intended relationship.

Next evaluate answer quality through the actual client, and use pilot feedback
to choose further changes. Existing vectors only reorder lexical candidates;
translation, broad paraphrases and general typo correction remain agent work.

## 0.10.0a3: title typos and batched variants

The conservative fallback adds one-edit **title/alias** candidates only
after the lexical and word-form stages are empty. Rules were fixed before
running the same 100-question diagnostic; no synonym list or language-specific
query correction was added. Compared with `63441e4`:

| Hybrid | Recall@5 | Precision | MRR@5 | Correct empty | Irrelevant results |
| --- | ---: | ---: | ---: | ---: | ---: |
| Word forms only | 0.427 | 0.727 | 0.420 | 24/25 | 12 |
| With title/alias typos | 0.600 | 0.789 | 0.593 | 24/25 | 12 |

Thirteen additional answerable questions find their source. Typo-category
recall rises from 3/18 to 16/18; the two explicit lexical modes stay unchanged.
These are single-query engine measurements, not model answer scores.

Run `python3 tools/measure_questions.py --set tools/typo_set.json` for a further
20 locally authored transfer cases created after fixing the rules, without
retuning them. Hybrid finds 14/14 answerable title/alias cases versus 0/14
previously; all six absent distractors stay empty. The set includes insertion,
deletion, substitution, transposition, aliases and Unicode. It is small and
authored by the same contributor, not an independent false-positive estimate.

`retrieve` now also accepts the original query plus three supplied variants.
Tests exercise translations, changed wording, ID deduplication, reciprocal
rank fusion, filters, shared body limits and one deadline across the entire
call. Each result retains its matching query origins and candidate kinds.
Translations remain caller-supplied; vector candidate membership is unchanged.
No new end-to-end model latency improvement is claimed from these engine and
protocol tests. See [ADR 0009](adr/0009-typo-and-query-variants.md).

## 2026-10-29: compound parts and body typos

Two further stages run only after every earlier stage is empty
([ADR 0010](adr/0010-body-typos-and-compound-components.md)): a query token of
six or more letters that opens or closes a searchable token at least three
letters longer (`match_kind: word_form`), then one-edit typos of body words of
seven or more letters (`match_kind: typo`). Compared with `3d3361d`, on the
100-question set:

| Hybrid | Recall@5 | Precision | MRR@5 | Correct empty | Irrelevant results |
| --- | ---: | ---: | ---: | ---: | ---: |
| With title/alias typos | 0.600 | 0.789 | 0.593 | 24/25 | 12 |
| With compound parts and body typos | 0.653 | 0.803 | 0.647 | 24/25 | 12 |

| Hybrid category (answerable) | Before | After |
| --- | ---: | ---: |
| literal (18) | 18 | 18 |
| temporal (3) | 3 | 3 |
| typo (18) | 16 | 18 |
| paraphrase_de (18) | 4 | 6 |
| cross_language (18) | 4 | 4 |

The four recovered questions are `Mondarchvi` and `Morgenstren` (spelled
correctly only in the body), `Zugreise schlafen` ("Nachtzugreise") and
`ruhiger Ort zum Kaffeetrinken` (body "ruhigen"). Substring and BM25 are
unchanged in every number on every set, as are hybrid's results on the
word-form transfer set (8/12 found, 8/8 empty, none irrelevant) and the
20-query regression set (hybrid Recall@5 and MRR 1.000, BM25 0.900,
substring 0.300).

**Changed label in the typo transfer set.** That set labelled its body case
`Dampfventli` (body "Dampfventil kontrollieren.") as an absent distractor,
because it was written to assert ADR 0009's title/alias-only rule. ADR 0010
makes that note the correct, labelled, last-resort candidate, so the query
is now answerable with that note as its answer. The label changed because the
policy changed, not to improve a number. With the new label, hybrid finds
14/15 before and 15/15 after. All 5 absent queries stay empty both times,
with no irrelevant result: two two-edit misspellings, one short word, and the
numeric and hash-like aliases. The set therefore no longer counts as transfer evidence for body
typos. The evidence is the 100-set recoveries above.

Each guard was compared against a looser setting on all three question sets:

| Guard | Looser setting | What the looser setting added |
| --- | --- | --- |
| body typo: 7 letters | 5 letters | two irrelevant results: `kalte` → "Karte", `nicht` → "Nacht" |
| body typo: 7 letters | 6 letters | no difference; 7 kept as the conservative choice |
| component: 6 letters | 5 letters | one irrelevant result: `Jacke` → "Regenjacke" for the zipper question |
| component: at the start or end | anywhere inside | no difference; anchoring kept |
| component: 3 extra letters | 2 extra letters | no difference |

An earlier draft also accepted only body words capitalised inside a sentence.
That guard was dropped: in German it means any noun not at a sentence start,
and it had been chosen while the `Dampfventli` label was in view. Dropping it
gains `ruhiger` and adds no irrelevant result on these sets.

Of the 26 answerable questions hybrid still misses, 12 are German paraphrases
and 14 are language changes with no lexical overlap, such as `bike chain care`
for "Fahrradwartung". `Pflanzen auf dem Balkon wässern` keeps its four
irrelevant results, because `auf` and `dem` match other entries and the
fallbacks never run after a nonempty union. These remain agent work through
`query_variants`; Noetrail adds no synonym list, translation or embeddings.
All three sets are locally authored by the same contributor and remain
diagnostics, not independent benchmarks.

## Fresh client check

On 2026-10-07 a ZeroClaw 0.8.4 client using `gpt-5.6-terra` with medium
reasoning was exercised against the same 21-entry synthetic corpus. Ten
ordinary user questions were sent as fresh single-message turns to the
knowledge agent and through the default agent's bounded delegation. The
model received the corpus and runtime prompts, not expected answers, query
variants or relevance labels. Tool evidence and final answers were inspected
outside the model. These are small maintainer-run examples, not an independent
benchmark, vendor GUI test or the volunteer pilot.

Initial runs exposed three response problems: a recipe stored as a note was
not read after a type-filter miss; scoped search failures became claims that
facts were not stored; and a delegated aggregate request became a title list.
The prompts were corrected to distinguish domain words from explicit stored
type restrictions, preserve source scope, and keep aggregate requests as
inventory calls. A further 20-turn cross-case run and four targeted fresh
counterchecks were used during correction. Earlier failures are not counted
as successful acceptance merely because the model process exited normally.

| Question class | Latest observed outcome |
| --- | --- |
| Aggregate inventory | 21 entries; one inventory call; no unsolicited title list |
| Exact name and German paraphrase | Answer grounded in the bicycle-care source body |
| Language change | English coffee question answered from the German source |
| Typo | Corrected spelling was actually searched before answering |
| Word-form candidate | Compound question answered from the labelled source candidate |
| Recipe stored as a note | Final targeted direct and delegated checks read the note and returned its title, type and contents |
| Historical relationships | The 2024 and 2025 questions used the corresponding validity intervals |
| Absent fact | Final targeted direct and delegated checks reported a bounded lookup failure without inventing a pressure value or claiming global absence |

All evaluated turns remained read-only. Temporary test prompts were restored
and their contents verified after each correction run. No private content,
credentials, deployment identities or raw traces are included here. Runtime
authentication, code isolation and host capability checks are separate from
answer grading. Gateway/CLI checks do not establish Matrix delivery or
end-to-end channel behavior.

The correction runs show why transport success and a single good answer are
insufficient. Model choices vary, and these prompts do not enforce answer
truth mechanically. Re-run the affected cases when changing the client,
model or prompt, retain failure examples, and complete the
[alpha pilot](alpha-pilot.md) before claiming uncoached usability.

`search --explain` / MCP `search` with `explain: true` adds `match_evidence` to
each returned entry: up to five excerpts of 240 characters, the matching field,
recorded origin, truncation flag, and body line where applicable. Empty queries
have no evidence. Fetched excerpts retain `origin: web`; missing field provenance
is `unknown`. Excerpts remain untrusted data. Explanations are opt-in so compact
lists do not start disclosing body excerpts unexpectedly. Evidence is computed
only for the returned page and does not alter ranking or membership.
Word-form candidates carry `match_kind: word_form` even without explanations;
their optional excerpts show observed supporting tokens rather than inventing
the queried spelling. `retrieve` preserves the candidate label with full bodies.

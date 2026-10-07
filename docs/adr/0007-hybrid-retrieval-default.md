# ADR 0007: The default text predicate is the union of both

- Status: accepted
- Date: 2026-08-19
- Supersedes: the "Substring stays the default" decision in
  [ADR 0005](0005-lexical-bm25-index.md)

## Context

[ADR 0005](0005-lexical-bm25-index.md) added BM25 as `--rank bm25` and kept
literal substring matching as the default. The reasoning was sound and is
still true: substring matching matches a prefix or an infix of a longer word,
which no tokenised ranking can do, so making BM25 the default would have taken
a real capability away.

What that reasoning treated as a two-way choice was measured on
`tools/retrieval_set.json`, and the two sides are not comparable in size:

| Mode | Recall@3 | Queries answered with nothing |
| --- | ---: | ---: |
| `substring` | 0.300 | 14 of 20 |
| `bm25` | 0.900 | 2 of 20 |

Substring returns nothing at all for 14 of 20 queries. That is not a defect;
it is what matching a literal substring means once a question has more than
one word. BM25 misses two, `hydrat` against "hydration" and `noodle` against
"noodles", which are exactly the infix and singular cases substring exists for.

This was not a theoretical cost. The default is what an agent gets when it
does not pass `rank`, and an agent asks in the user's words. A search that
returned nothing was reported to the user as "you have none" for entries that
existed — the same failure mode `without_type_filter` was added to catch,
arriving through the text predicate instead of the type filter.

## Decision

**`--rank hybrid` is the default.** It runs both existing predicates and
returns the union, ordered by BM25 relevance.

It adds no third way of matching. There is no new scoring function, no
stemmer, no synonym list, and no heuristic that decides which mode a query
"looks like" — anything of that kind would be a fourth thing to be wrong and
would make the answer depend on a guess about the query. The two engines are
unchanged; only membership of the candidate set changes.

**A ranked match sorts above a merely literal one.** Every BM25 score is
strictly positive, because ADR 0005 chose the Lucene IDF that never goes
negative, so a literal-only match takes 0.0 and lands below every ranked match
without a sentinel. This ordering is deliberate: a substring can fall inside a
word that means something else, which makes a literal hit worth returning and
not worth returning first.

**Both single modes stay reachable.** `--rank substring` and `--rank bm25` are
unchanged and remain in the CLI, the MCP schema, and saved views. A caller who
wants the cheapest possible scan, or a ranking with no literal half, still has
it.

## Consequences

Measured on the same set, hybrid answers every query and is better than either
mode at rank 1:

| Mode | Recall@1 | Recall@3 | MRR |
| --- | ---: | ---: | ---: |
| `substring` | 0.275 | 0.300 | 0.300 |
| `bm25` | 0.825 | 0.900 | 0.900 |
| `hybrid` | 0.925 | 1.000 | 1.000 |

The 1.000 means "no query in a set of 20, written in this repository, survived
both routes". It is not a claim about retrieval in general and no external
benchmark is claimed; see [Limits and scaling](../limits.md).

**It costs both scans.** A hybrid query pays the literal scan plus the ranked
one. On the measured hardware that is milliseconds up to a few thousand
entries and matters at 50,000, where it is the argument for building the index
rather than for changing the default back. `--rank substring` remains the
cheapest option for anyone who wants it.

**A stored query can change its answer.** That is the point, and it is why
this is a decision record rather than a bug fix: prompts and saved views
written against substring matching will now return more, including entries
they were previously silent about. Nothing an agent could previously find is
lost, because the substring candidate set is contained in the hybrid one.

**Relevance ordering follows the default.** `--sort auto` resolves to
relevance under any ranking mode with a non-empty query, so an ordinary search
now comes back ranked rather than newest-first. An empty query still has no
ranking to show and keeps the recency order.

## Alternatives rejected

**Make `bm25` the default.** Simpler to explain and it drops the infix
capability that ADR 0005 protected. The two queries it misses are ordinary
ways to type a search, not corner cases.

**Fall back to substring only when BM25 returns nothing.** Cheaper, and wrong
in the case that matters: a query where ranking returns one weak hit and the
literal scan would have returned the right entry never reaches the fallback.
The union has no such threshold to tune.

**Add a stemmer instead.** It would answer `noodle` against "noodles" and
would not answer `extractio` against "extraction". A stemmer that is right for
English is wrong for German compounds, and ADR 0005 rejected it for the same
reason.

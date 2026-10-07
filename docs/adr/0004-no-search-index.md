# ADR 0004: No search index before a measured trigger

- Status: superseded by [ADR 0005](0005-lexical-bm25-index.md)
- Date: 2026-08-03
- Roadmap phase: 12

> **Superseded.** The trigger this record names was reached, and re-measurement
> found a second problem it had not weighed: substring matching returns nothing
> for an ordinary multi-word query, which no amount of scan speed fixes.
> [ADR 0005](0005-lexical-bm25-index.md) records the index that was built, the
> numbers behind it, and which of the constraints below it keeps — all of them,
> plus the rejection of a semantic index as the source of a ranking. The text
> below is left exactly as it was written, because a record of a decision is
> worth nothing once it is edited to agree with the next one.

## Context

Search parses every active Markdown entry for each query. That is linear in
entry count and in combined text size, and it is the obvious thing to fix with
an index. The question is not whether an index would be faster — it would — but
whether it is faster in a way anyone notices, and what it costs permanently.

Markdown is the source of truth. Any index is therefore a derived copy that has
to stay consistent with files a user may edit in an editor, restore from a
backup, or sync between machines. It must be rebuildable and safely deletable
at any time, and it must never become required for a restore. That obligation
does not expire once it is taken on.

The numbers are from `tools/measure_search.py`, recorded in
[Limits and scaling](../limits.md): a synthetic vault of short thoughts with a
twelve-word body, median of five runs on an Apple M-series laptop with a local
SSD and CPython 3.13.

The linear scan costs about **27 µs per entry**:

| Entries | In-process scan |
| ---: | ---: |
| 100 | 2 ms |
| 1,000 | 25 ms |
| 5,000 | 134 ms |

Until [ADR 0003](0003-in-process-reads.md) that scan was not the dominant cost
at all. A fixed ~47 ms of interpreter startup and import per call was, and an
agent turn pays it once per call:

| Entries | Six-call turn, subprocess per read | Six-call turn, in-process |
| ---: | ---: | ---: |
| 100 | 316 ms | 32 ms |
| 1,000 | 494 ms | 199 ms |
| 5,000 | 1,350 ms | 1,049 ms |

After that change the remaining turn latency *is* the scan. At 5,000 entries a
representative turn is about 1,049 ms, of which the constant per-call overhead
is only about 300 ms.

## Decision

Do not build a search index now. Record the trigger that would justify one, so
the decision is revisited on evidence rather than on the next time search feels
slow.

**Trigger.** Re-run `tools/measure_search.py` when either holds:

- the vault approaches **5,000 entries**, which is where the scan alone pushes
  a representative six-call turn past one second; or
- a representative turn exceeds **one second** in practice at any entry count,
  which happens earlier with long bodies, because the scan loads entry text.

Only then design an index, and only under the constraints below.

**Constraints on any future index.** Markdown stays authoritative. The index
must be fully rebuildable from the vault, safely deletable at any time, never
required for a restore, and never a second place where a user-visible value is
stored. An entry that is missing from the index is a stale index, not a missing
entry.

## Consequences

- Search stays a plain linear scan with no second copy of the data, no rebuild
  step after an external edit, no invalidation logic, and no failure mode where
  search and the vault disagree.
- Restore stays a file operation. Copying the data root back is sufficient.
- Above roughly 5,000 entries, or with large bodies below that, search is
  visibly slow. [Limits and scaling](../limits.md) states this as the edge of
  the default unindexed profile and asks for a benchmark before exceeding it,
  rather than presenting a hard ceiling the code does not enforce.
- Nothing about this decision is irreversible: no on-disk format, no
  configuration key, and no documented behaviour depends on the absence of an
  index.

## Alternatives considered

**Build an index now.** Rejected. Below 5,000 entries the saving is a fraction
of a second that a user cannot perceive, while the consistency obligation is
permanent and starts immediately.

**A cache keyed by file modification time.** Rejected as a smaller version of
the same problem: it still needs invalidation, still disagrees with the vault
after an out-of-band edit, and buys nothing on a first query — which is the one
an agent makes.

**SQLite as the source of truth, with Markdown exported.** Rejected outright.
It ends the property the project is built on: that a vault is readable, and
repairable, with an editor and no program.

**A semantic or vector index.** Rejected for the same reason plus one more: it
would make results depend on a model version, so the same query would return
different entries after an upgrade, and it cannot be reproduced by a reader
inspecting the files.

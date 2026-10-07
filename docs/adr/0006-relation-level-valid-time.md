# ADR 0006: Valid time and supersession live on the typed relation

- Status: accepted
- Date: 2026-08-07
- Roadmap phase: 14

## Context

A vault records what its owner wrote down. It has never recorded what an entry
*replaced*. "Alex works at Orbit Works" and "Alex works at Harbor Labs" can
both be true, at different times, and the vault had exactly two ways to hold
them:

- `update` overwrote the older statement, and it was gone. Nothing said it had
  ever been true, and nothing said when it stopped being true.
- A second entry was captured, and both statements then stood next to each
  other as equals. `search` returned both, `relations` returned both, and a
  reader had no way to tell which one currently holds.

Two capabilities are missing behind that, and they are separable:

**Valid time.** An entry carries `created_at` and `updated_at`. Both are
*record time*: when the vault first held the entry, and when it last changed.
They are honest about that, and they are the wrong clock for "since when did
this hold in the world". Nothing in the format could express the second clock
at all.

**Supersession.** Even with intervals on both statements, "the second one
replaced the first" is a fact of its own. Without it there is no way to tell a
replacement apart from two unrelated statements that happen to abut, no way to
walk from a current statement to the one it replaced, and no way for `validate`
to know which two intervals are supposed to be about the same thing and must
therefore not overlap.

The constraint from [ADR 0001](0001-core-and-schema-packs.md) applies
unchanged: the pack format is declarative and not executable. Anything the
packs would have to *evaluate* rather than *declare* cannot live in a pack.

## Decision

**Valid time and supersession live on the individual typed relation.**

A relation object, which until now was exactly `{"predicate", "target"}`, may
additionally carry:

| Key | Meaning |
| --- | --- |
| `valid_from` | the instant from which this assertion holds in the world |
| `valid_until` | the instant from which it no longer holds (half-open) |
| `superseded_by` | the target ID of the same-predicate relation that replaced it |
| `recorded_at` | the instant this assertion entered the vault |

All four are optional and absent by default. A relation without bounds holds at
every instant, so every entry written before schema 11 answers exactly as it
did. The identity of an assertion inside one entry is `(predicate, target)`;
`superseded_by` names a successor with the same predicate on the same entry, so
a supersession chain is walkable and checkable without a second index.

`relate --supersedes <old-target> --valid-from <instant>` writes both halves in
one file write: the replaced relation is closed at that instant and marked, and
the replacement is added starting there. **The replaced relation is not
deleted.** It stays in the Markdown, readable by any tool, which is the
difference between recording a change and overwriting a value.

Every read takes one instant — `--as-of`, defaulting to now — and evaluates
relation validity against it. `search` matches relation filters only against
assertions in force then; `relations`, `get_entry` and `inventory` split their
output into what is in force and what is not.

Comparisons run over instants (`datetime.timestamp()`), never over the stored
strings, and a timestamp without an offset is refused rather than assumed to be
local. `now_iso()` writes local time with an offset, so
`2026-03-01T00:30:00+02:00` sorts *after* `2026-02-28T23:30:00+00:00`
lexicographically while naming the earlier instant.

Schema 11 makes the widened relation object legal. The 10-to-11 migration adds
no interval to any existing relation.

## Alternatives considered

### The whole entry carries the interval

Every entry would gain optional `valid_from` / `valid_until` and an
entry-level `superseded_by` pointing at another entry.

Rejected. Under it, "Alex works at Orbit Works" has to *become an entry* before
it can be superseded — with its own title, body, path, and revision — and
changing employer means capturing a second such entry. A person's employment
history becomes a pile of near-empty entries whose titles restate their single
relation. It also creates a second graph: relations already say who is
connected to whom, and entry-level `superseded_by` would say which entry
replaced which, with no rule connecting the two. And it makes the common case
worse rather than better: the overwhelming majority of entries are notes,
bookmarks and thoughts that hold no time-bounded assertion at all, and the
brief was explicitly not to inflate them.

### The individual attribute field carries the interval

`attributes.employer` would carry its own history, the way `provenance`
carries a per-field origin today.

Rejected for three reasons. First, it collides with the pack boundary: an
attribute's shape is declared by `pack.yaml`, and a per-field history would
either have to be declared there — turning a data format into something with
evaluation rules — or bolted on beside it, so the same field would be described
in two places. Second, `attributes` is a JSON object validated as a whole
against a generated JSON Schema; a parallel `attribute_history` map would be a
second store for the same values, and the two would drift. Third, and most
importantly, it answers the wrong question. A superseded fact is almost always
a *link* — employer, membership, ownership, location — and those are already
typed relations. Modelling them as scalar attribute values to give them history
would push structure the vault already has back into free text.

### A dedicated lightweight statement type

A `statement` entry type: subject, predicate, object, interval, provenance,
supersession. The classical reified triple.

Rejected as the parallel model this was explicitly meant to avoid. It would
need its own storage location, its own search behaviour (a statement is not a
document and would pollute BM25 results with near-empty text), its own review
and trash lifecycle, and its own relationship to the ordinary relations that
express the same edges today. Two ways to say "Alex works at Harbor Labs" is
strictly worse than one, and the migration question — which existing relations
become statements — has no non-arbitrary answer.

### Interval on the relation, supersession by ordinal

Keep intervals on relations but identify the replaced relation by its position
in the `relations` array instead of by its target.

Rejected. An ordinal is not stable: removing an earlier relation silently
re-points every supersession link below it, and the frontmatter would then
claim a replacement nobody recorded. `(predicate, target)` is already unique
within an entry — `relate` refuses a duplicate — and survives reordering.

### Full transaction-time history

Keep every past version of an entry, so any earlier `updated_at` can be
reconstructed.

Not done, and deliberately deferred. The vault would have to keep either a log
or per-entry snapshots, both of which are a second store beside the Markdown
and would break "the files are the truth". Record time is kept at the
resolution the format can carry honestly: `created_at` and `updated_at` for the
entry, `recorded_at` for the individual assertion. Point-in-time queries
therefore answer over valid time only, and the documentation says so rather
than implying a history that is not kept.

## Consequences

**What gets better.** A replaced statement stays readable in the Markdown next
to the one that replaced it, with the instant the change took effect. Reads
return the state that currently holds without the caller asking for it, and any
past state on request. `validate` and `doctor` report contradictions that were
previously invisible: an assertion that still runs while its replacement
already does, a supersession chain that loops, and a replacement dated before
the statement it replaces began.

**What it costs.** The relation object is no longer a fixed pair, so anything
comparing relation dictionaries for equality has to compare identity instead —
that was a real defect class during this change, and `find_edge` exists to make
the correct comparison the easy one. `relation_count` in `search` and
`inventory` now means "in force at the queried instant" rather than "stored",
which is a behaviour change for any entry that carries intervals; the stored
total is reported next to it as `recorded_relation_count`.

**What it does not do.** Only relations carry validity. Entries do not, so no
entry is ever hidden by a point-in-time query — the change is in which
structured facts hold, not in which documents exist. Supersession is an editing
operation (`relate`), not a capture-time one: an assertion has to exist before
something can replace it.

**Interaction with the BM25 index.** None, by construction. The index stores
term statistics for title, declared searchable fields, tags and body;
relations are not searched text and are not in it. A point-in-time query
therefore changes which *structured* facts hold, never which terms an entry
contains, so it needs no index format change, cannot make an index stale, and
runs as a filter on whatever the text stage produced. Both the indexed and the
scanned path read the entry's frontmatter before that filter runs, so both see
the same relations and return the same answer — the equivalence
[ADR 0005](0005-lexical-bm25-index.md) requires is unaffected. This is why
point-in-time did not need to force the linear path.

## Revisit when

- An entry-level statement (not a link) needs a validity interval — a status
  or a rating that changed at a known instant. That is the first case this
  granularity cannot express, and it is the trigger to reconsider the
  attribute-level alternative rather than to bolt an entry-level interval on
  beside this one.
- A user needs to ask "what did the vault believe on 3 March", not "what held
  in the world on 3 March". That is the transaction-time history deferred
  above, and it is a storage decision, not an extension of this one.

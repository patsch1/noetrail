# Finding and merging related knowledge

`noetrail candidates <id>` compares a selected entry with the active vault.
Shared titles or aliases, equal canonical/source URLs, or at least two shared
title words produce suggestions with reasons. The command returns no bodies,
does not infer identity, and never changes an entry. Page with `--limit` (1–50)
and `--offset`; inspect `complete` before concluding no candidates exist.
The MCP equivalent is `find_candidates`.

When two entries are confirmed duplicates, preview:

```sh
noetrail merge <source-id> <target-id>
```

The plan includes the target body and metadata, affected IDs, source retention,
and an `expected_plan` digest. Applying requires that exact digest:

```sh
noetrail merge <source-id> <target-id> --apply --expected-plan sha256:<digest>
```

MCP uses `merge_entries` with `source_id`, `target_id`, and, after preview,
`apply: true` and `expected_plan`. Both preview and apply run in the existing
isolated CLI process. They do not get a longer mutation timeout.

The target keeps its ID, title and creation time. Source text is appended under
a heading containing the source ID. Tags, existing aliases, attachments and
relations are combined; incoming structured relations follow the target.
The result becomes unreviewed. The source keeps its own ID, original body and
metadata in reversible trash for 90 days, with the normal trash lifecycle
fields. Inline prose and Markdown links are not rewritten.

This is deliberately conservative. Both entries must use schema 12; differing
types, sensitivity, attributes, source records or other non-combinable fields
are refused. Provenance-marked entries and fetched body sections are refused
because concatenation could mislabel their origin. Self-relations, conflicting
edges, invalid resulting metadata and unreadable vault files also block a merge.
Review these cases manually; do not discard fields to make a merge succeed.

The digest covers both selected entries and the active vault snapshot so a
changed incoming edge invalidates the plan. Apply holds the exclusive vault
lock, writes each file atomically and rolls back original bytes on exceptions.
It is **not a crash-atomic transaction across files**: process termination or
power loss can leave partial work and `.merge-*` recovery copies beside entries.
The source is moved last. After an interruption stop writes, retain those copies,
validate, inspect the two entries and affected edges, and recover from the
paired backup if necessary. Do not blindly repeat the merge: already appended
text could otherwise be duplicated. Restoring the source from trash alone does
not undo copied text or redirected edges; a full undo uses the pre-merge backup.

No new on-disk fields or implicit migrations are introduced.

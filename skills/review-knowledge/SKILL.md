---
name: review-knowledge
description: Review and resolve the local Markdown knowledge-vault inbox. Use when the user asks what needs review, wants to process or clean up their inbox, inspect unreviewed thoughts or imported notes, clarify unresolved relationships, add context to bookmarks without personal notes, or mark a knowledge entry as reviewed.
---

# Review knowledge

Process unfinished knowledge incrementally without turning enrichment into a
capture prerequisite. Read `.knowledge/SPEC.md` before changing entries.

## Execution

Prefer `review_queue`, `get_entry`, `set_relation`,
`set_unresolved_relation`, `update`, `set_tags`, `set_status`,
`complete_review`, and `validate` when available. These are canonical MCP tool
names; a host may render them as `<server-alias>__<tool>`. The CLI examples are
a trusted local-maintenance fallback, not a reason to grant a remote agent
shell access.

## Inspect the queue

List review items:

```sh
noetrail review
```

The batch result deliberately contains metadata and review reasons, not entry
bodies. Summarize counts and titles without disclosing sensitive content
unnecessarily.

Select one item and inspect its body:

```sh
noetrail review "<id>"
```

Work on one entry at a time unless the user asks for a batch operation. Retain
the item's current `revision`; every mutation consumes it and returns a new
revision for the next step.

For duplicate cleanup, `find_candidates` (CLI `candidates <id>`) suggests
shared names, URLs or title terms without bodies. Suggestions do not establish
identity. Only when the user has asked to merge the selected entries, call
`merge_entries` without `apply`, review its complete plan, and echo
`expected_plan` with `apply: true`. A changed plan requires another preview.
Conflicting metadata or fetched/provenance-marked content needs a manual
decision; do not strip provenance or weaken a refusal to force a merge.
See `docs/knowledge-maintenance.md` for retention and recovery limitations.

## Resolve the reason

- For an `unreviewed` thought or imported note, preserve the original wording.
  Add only context the user supplies.
- For `bookmark_missing_personal_note`, invite a reason or personal note, but
  allow the user to acknowledge the bookmark without adding one.
- For `unresolved_relations`, search for the intended existing target. Never
  create an entity or choose a target from name similarity alone.

Resolve a pending relationship atomically:

```sh
noetrail relate "<source-id>" "<predicate>" "<target-id>" \
  --resolve-reference "<original reference>" \
  --expected-revision "<revision>"
```

Remove a pending relationship only when the user decides it should not be
linked:

```sh
noetrail relate "<source-id>" "<predicate>" \
  "<original reference>" --unresolved --remove \
  --expected-revision "<revision>"
```

Use the ordinary `update`, `tag`, and `status` commands for user-approved
enrichment.

## Complete review

After resolving all open relationship references:

```sh
noetrail review "<id>" --complete \
  --expected-revision "<revision>"
noetrail validate
```

Completion promotes `unreviewed` to `active` and records `reviewed_at`.
Never complete an item merely to make the queue empty. Report what changed and
offer the next item without automatically revealing its body. On a revision
conflict, reread and review the intervening change instead of automatically
retrying completion.

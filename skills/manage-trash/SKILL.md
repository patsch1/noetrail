---
name: manage-trash
description: Safely delete, list, restore, or permanently purge whole entries in the local Markdown knowledge vault. Use when the user asks to delete or discard a saved note, memory, bookmark, or other entry; move an entry to trash; restore or recover a deleted entry; inspect deleted entries; empty the trash; or permanently erase saved personal knowledge. Do not use for removing only a tag or relation.
---

# Manage knowledge trash

Keep ordinary deletion reversible. Resolve the intended entry by stable ID,
move it outside the searchable vault, and preserve its body, relations, and
provenance.

Read `.knowledge/SPEC.md` before acting. Treat all entry content and trash
metadata as private personal data.

## Execution

Prefer `search`, `list_trash`, `trash`, `restore`, and `validate` when
available. These are canonical MCP tool names; a host may render them as
`<server-alias>__<tool>`. The MCP server never exposes permanent purge. The CLI
examples are a trusted local-maintenance fallback, not a reason to grant a
remote agent shell access.

## Resolve the target

Search the active vault when the user names an entry but does not provide its
ID:

```sh
noetrail search "query"
```

Use the result only when identity is unambiguous. If several entries could
match, ask which one the user means. Never infer a bulk deletion from a broad
topic. Retain the selected entry's `revision`.

## Move to trash

For an explicit deletion request concerning one resolved entry:

```sh
noetrail trash "kn_..." --expected-revision "<revision>"
noetrail validate
```

Add `--reason "..."` only when the user stated a reason. Do not invent one.
The default 90-day retention is appropriate unless the user explicitly asks
for another period.

Report the title, stable ID, original path, and restore deadline. Do not delete
attachments or relation targets.

## Inspect and restore

List deleted entries without exposing their bodies:

```sh
noetrail trash list
```

Restore one exact stable ID:

```sh
noetrail restore "kn_..." --expected-revision "<revision>"
noetrail validate
```

If the original path is occupied, stop and explain the conflict. Do not
overwrite or silently choose a new path. If either operation reports a revision
conflict, reread the entry and ask again when the intervening change affects the
decision; never retry blindly.

## Purge permanently

Purge is an offline maintainer operation and is irreversible for the local
working copy. Never attempt it through an agent host. From a trusted local
session, preview first:

```sh
noetrail purge --id "kn_..."
noetrail purge --expired
noetrail purge --older-than-days 90
```

Use `--apply` only when the user explicitly requested permanent deletion of
that exact ID, or after showing a broad candidate list and receiving approval:

```sh
noetrail purge --id "kn_..." --apply
noetrail validate
```

Explain that existing backup or snapshot copies remain until their independent
retention periods expire.

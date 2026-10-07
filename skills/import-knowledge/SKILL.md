---
name: import-knowledge
description: Preview and import an Obsidian vault, a Basic Memory project, or generic Markdown, and inspect and normalize Notion, Joplin, and other personal note exports into the local Noetrail vault while preserving provenance, attachments, links, and original data. Use when the user asks to migrate, import, transfer, inspect, or trial-convert Markdown files or an exported note library.
---

# Import Knowledge

Operate on the workspace containing `.knowledge/SPEC.md`. Read its import rules
before changing files.

This is a trusted local-maintenance skill and should not be installed in an
unattended remote-agent profile. The Noetrail MCP server does not expose raw
imports or schema migration. Use the canonical MCP tool `validate` when it is
available (a host may prefix it with its server alias); otherwise use the local
validation command below.

## Workflow

1. Identify the export format, file count, attachment layout, internal links,
   source IDs, timestamps, and any databases or notebooks.
2. Place or retain an unchanged, dated copy below `imports/raw/<source>-<date>/`.
   Never edit the raw copy.
3. Check for secrets, unusually sensitive areas, very large attachments, and
   formats that cannot be represented safely. Report these before a bulk run.
4. For generic UTF-8 Markdown, run a non-writing preview first:

   ```sh
   noetrail import markdown --source <raw-relative-path> --limit 10
   ```

   The source must be below `imports/raw/`. Review every skip, conflict, and
   error, then apply the same selection explicitly with `--apply`. Omit
   `--limit` only for the reviewed full run.
5. For an Obsidian vault or a Basic Memory project, use the dedicated
   importer instead of the generic one, because it converts wikilinks, tags,
   and embedded images:

   ```sh
   noetrail import obsidian --source <raw-relative-path> --limit 10
   noetrail import basic-memory --source <raw-relative-path> --limit 10
   ```

   Read the report's `not_imported`, `rejected`, and `unresolved_link_targets`
   sections aloud to the user before applying. `--strict` refuses the whole
   run instead of skipping unreadable notes.
6. For other source-specific exports, normalize no more than ten
   representative entries first.
7. Use `note` and `status: unreviewed` unless another type is unambiguous.
   Preserve clear but unresolved relationships in `unresolved_relations`
   instead of fabricating target IDs.
8. Preserve provenance using the `source` object defined in
   `.knowledge/SPEC.md`. Use original source IDs to make repeated imports
   idempotent.
9. Preserve the original wording, headings, checklists, timestamps, internal
   links, and attachment relationships. Do not "improve" the prose.
10. Run:

   ```sh
   noetrail validate
   ```

11. Present a trial-import report: created entries, duplicates, unresolved
   links, missing attachments, unsupported blocks, and any lossy conversion.
12. Continue with a full import only after the trial output has been reviewed.

## Source guidance

- For Notion, account for nested pages, database properties, page IDs, exported
  filenames, CSV tables, and attachment directories.
- For Joplin, account for notebook hierarchy, note IDs, Markdown resources,
  tags, created/updated times, and internal `:/resource-id` references.
- Do not design a complete importer from assumptions. Inspect a real export
  sample and add deterministic conversion code only for observed structures.
- The generic Markdown importer preserves Markdown and provenance but does not
  copy linked attachments or infer relations. Use `import obsidian`,
  `import basic-memory`, or a tailored importer when those relationships
  matter.

## Safeguards

- Do not overwrite raw exports.
- Do not delete source items after import.
- Do not infer relations or identities from similar names alone.
- Do not treat an import as complete while attachments or links are silently
  missing.
- Keep sensitive content private and out of illustrative reports.

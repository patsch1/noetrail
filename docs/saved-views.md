# Declarative saved views

Saved views give a stable name to a bounded Noetrail search. They live in the
instance configuration at `<config-root>/views.yaml`; they are not vault data,
do not contain entry bodies, and do not change the Markdown format.

```yaml
format_version: 1
views:
  unread_articles:
    title: "Unread articles"
    description: "Articles retained for later."
    query: ""
    type: "bookmark"
    bookmark_kind: "article"
    reading_status: "unread"
    sort: "updated_desc"
    limit: 20
```

Names use lowercase letters, digits, and underscores and begin with a letter.
At most 128 views may be configured. A view may declare the same bounded
filters as search: query, type, searchable custom attributes, bookmark and
experience facets, relation filters, date bounds, point-in-time evaluation,
sort, rank, and a page limit from 1 to 50. `limit` and `offset` may be
overridden by the caller when the view is run.

Use `noetrail view list` to inspect the configured definitions and
`noetrail view run unread_articles` to execute one. MCP clients receive the
equivalent read-only `list_views` and `run_view` tools.

The file uses the same restricted, dependency-free YAML subset as schema
packs. Unknown keys, symbolic links, executable hooks, paths, and unbounded
values are rejected. A view can only supply arguments to Noetrail's existing
read-only search implementation; it cannot run shell, Python, templates,
network requests, or mutations. Removing `views.yaml` removes every saved view
without touching the vault.

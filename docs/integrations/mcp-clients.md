# Connecting an MCP client

Noetrail ships a dependency-free MCP server over newline-delimited JSON-RPC on
stdio. The vault format and the core tools do not depend on ZeroClaw, Matrix,
or any other agent host.

## Tool names and host prefixes

The server publishes canonical tool names. A client commonly prepends the
configured server alias, so the same `search` tool may appear as
`noetrail__search`, `knowledge__search`, or simply `search`. The prefix is a
host convention, not part of Noetrail's protocol contract. Portable prompts
and skills refer to the canonical names; host-specific instructions may use the
rendered form.

The current surface is:

| Kind | Canonical tools |
| --- | --- |
| Schema discovery | `list_types`, `describe_type` |
| Read and review | `inventory`, `search`, `retrieve`, `list_views`, `run_view`, `get_entry`, `review_queue`, `relations`, `list_trash`, `validate` |
| Capture and update | `capture`, `save_recipe`, `save_bookmark`, `refresh_bookmark`, `update` |
| Attachments | `list_pending_attachments`, `add_attachment`, `get_attachment` |
| Structure and status | `set_relation`, `set_unresolved_relation`, `set_tags`, `set_status`, `complete_review` |
| Knowledge maintenance | `find_candidates`, `merge_entries` |
| Lifecycle | `trash`, `restore` |

There is no `relate` or `tag` MCP tool: those are CLI command names. MCP uses
`set_relation` and `set_tags`. The server exposes no shell, arbitrary path,
schema migration, body replacement, or permanent purge. Mutations delegate to
the CLI, which remains the single implementation of vault rules.

`retrieve` combines a search and up to ten selected full-entry reads in one
round trip, with a shared body budget capped at 100,000 characters. Use it when
answer synthesis needs several bodies; use compact `search` for lists and
selection. `list_views` and `run_view` expose only the bounded searches from
the instance's declarative `views.yaml`.

For alternative wording or a language change, pass `query_variants` alongside
the original `query`, for example (available since 0.10.0a3):

```json
{"query": "cold proofing", "query_variants": ["Teigruhe", "Teig kalt gehen lassen"], "limit": 5}
```

This is one `retrieve` call with three independent query strings, deduplicated
entries and one shared body budget. Noetrail does not translate automatically.
The response adds `queries`, `query_fusion` and each entry's `query_matches`.
The latter retains candidate labels such as `typo`; inspect the actual body
before answering. Preserve user-supplied type and date restrictions across all
variants. A four-query agent budget counts each string, not just each tool call.

## Protocol compatibility

Noetrail supports both MCP handshake eras without changing the tool surface:

| Client handshake | Version behavior | CI coverage |
| --- | --- | --- |
| `initialize` | negotiates the deployed `2024-11-05` protocol; later initialize-era clients may accept this supported downgrade | ZeroClaw legacy profile plus non-ZeroClaw desktop/editor profiles |
| `server/discover` with per-request `_meta` | native `2026-07-28`; advertises both supported versions and returns complete/cache metadata | discovery, list, call, malformed metadata, and unsupported-version tests |

The two non-ZeroClaw profiles exercise the wire behavior used by a desktop host
and an editor host. They do not automate a vendor GUI or promise that every
future release of that vendor accepts the same configuration. When reporting a
client issue, include the client version, requested protocol version, and the
server alias shown in that client.

## Install and get the configuration block

The alpha is on PyPI; its
[installation command](../installation.md#install-single-user-own-machine)
installs the same CLI and MCP entry points. Use the pinned alpha version:

<!-- docs-check: skip - uvx installs from a package index over the network -->

```sh
uvx --from 'noetrail==0.10.0a3' noetrail quickstart
```

From a checkout as an alternative:

<!-- docs-check: skip - installs the checkout -->

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/noetrail quickstart
```

`quickstart` prints a server definition with absolute data and configuration
roots. Its `--json` form returns it under `mcp_config`. The generated command is
`noetrail-mcp`; it does not install that command on the client host's `PATH`.

If you used `uvx` for quickstart, launch the MCP server through `uvx` too:

```json
{
  "mcpServers": {
    "noetrail": {
      "command": "uvx",
      "args": [
        "--from", "noetrail==0.10.0a3", "noetrail-mcp",
        "--data-root", "/absolute/data", "--config-root", "/absolute/config"
      ]
    }
  }
}
```

Replace the roots with the paths returned by quickstart. Use the absolute
`uvx` path if the client cannot resolve it. This launches the published alpha;
it does not require a separately installed `noetrail-mcp` executable.

## Configure a desktop or editor host

Most hosts accept this JSON shape. The key `noetrail` is only the local alias;
changing it changes the rendered tool prefix but not the canonical names:

```json
{
  "mcpServers": {
    "noetrail": {
      "command": "noetrail-mcp",
      "args": [
        "--data-root", "/Users/you/.local/share/noetrail",
        "--config-root", "/Users/you/.config/noetrail"
      ]
    }
  }
}
```

If the host cannot resolve console scripts on its `PATH`, invoke the installed
module with an absolute interpreter path:

```json
{
  "mcpServers": {
    "noetrail": {
      "command": "/absolute/path/to/python3",
      "args": [
        "-m", "noetrail.mcp",
        "--data-root", "/Users/you/.local/share/noetrail",
        "--config-root", "/Users/you/.config/noetrail"
      ]
    }
  }
}
```

Use absolute paths. The server resolves its roots at startup and refuses to
follow symlinks out of them. Reads run in the server process; mutations and
`validate` keep their separate-process timeout. `--subprocess-reads` is the
slower process-isolated fallback.

## Portable skills and host overlays

The workflows in `skills/` use canonical MCP names and standard MCP content
blocks. They contain no ZeroClaw delegation policy, Matrix marker, or fixed
server alias. A host integration may add an overlay for its own routing and
delivery behavior. The ZeroClaw overlay is `deploy/zeroclaw/AGENTS.md`; it is
not required by the core package or by other MCP clients.

## Optimistic concurrency

Every read returns a `sha256:...` revision. A mutation of an existing entry
requires that exact value as `expected_revision`. On conflict, reread and
review the intervening change; do not retry a stale mutation.

## Attachments

To find image-bearing entries, call `search` with `query: ""` and
`has_attachment: true`. The optional boolean also works with `retrieve`;
`false` selects entries with no attachments. CLI equivalents use
`--has-attachment` or `--no-has-attachment`. Combine it with existing filters,
and follow `has_more` / `next_offset` for a complete search listing. Compact
results contain `attachment_count`; read `get_entry` for attachment IDs.
`inventory` reports `entries_with_attachments` and `attachment_count` across
active entries. These count stored references, including repeated use of a
blob; they do not classify image contents or count distinct photographs.
An incomplete inventory cannot establish that no attachments exist.

Attachment ingestion is disabled unless the server starts with
`--attachment-inbox /absolute/path`. `add_attachment` accepts only a
host-supplied path beneath that fixed inbox or a short-lived token returned by
`list_pending_attachments`; it never reveals the inbox path.

`get_attachment` returns the stored image as a standard MCP image content
block plus a text/structured summary. A capable client can display that block
directly. `--attachment-outbox` optionally copies a deliverable image into a
fixed host workspace and adds `delivery_path` to the summary. A host whose
channel adapter requires exact text syntax can additionally set
`--attachment-delivery-marker-template`, with one `{path}` placeholder; the
summary then contains the complete `delivery_marker` for an agent to copy
verbatim. By default that placeholder receives the absolute `delivery_path`.
If a channel resolves paths relative to a fixed workspace, set
`--attachment-delivery-marker-root` to that workspace; Noetrail then verifies
that the outbox is inside the root and puts only the safe relative path in the
marker. Use this relative form for ZeroClaw Matrix. ZeroClaw v0.8.4 Discord
requires an absolute path inside its agent workspace: set the template to
`[PHOTO:{path}]` and omit the marker root. Its sender accepts `PHOTO` as an
image alias, while the agent's image-input parser consumes absolute `IMAGE`
markers before delivery. See the channel-specific examples in
[ZeroClaw setup](zeroclaw-setup.md). No template is enabled by default, and
portable Noetrail skills define none. Stored blobs remain private data and
follow the vault's backup and retention policy.

## Web content

The knowledge server has no network access by design. Bookmark enrichment uses
the separate `noetrail-bookmark-fetcher`, which has no vault access and returns
allowlisted metadata rather than page bodies. Keep that trust boundary intact:
giving a knowledge-capable agent a general web tool reintroduces prompt
injection into the write path.

For an existing bookmark, read `get_entry` for its current revision, delegate
the saved URL to the isolated fetcher, then call `refresh_bookmark` with `id`,
`expected_revision` and `envelope` containing the entire fetcher reply. It fills
missing page metadata with `web` provenance and classifies only an `unknown`
kind. Existing title, site name, authors, tags and body remain unchanged;
retrieval status and timestamp come from the reply, including failures.
The request URL must match the saved original or canonical URL. No fetching
occurs in this tool. Its CLI equivalent is `noetrail refresh-bookmark ID
--expected-revision REVISION --metadata-file envelope.json`. Refresh requires
mutation approval; a permitted ZeroClaw session can authorize a batch.
Schemas 10–12 support this operation; older entries require explicit migration
before page fields can retain their provenance.

## Existing vaults and hardened deployments

`noetrail import obsidian` and `noetrail import basic-memory` operate on a copy
below `<data-root>/imports/raw/`, preview by default, and write only with
`--apply`. See [Importing notes](../importing.md).

A container deployment is optional. For the supported ZeroClaw profile with a
chat channel, network policies, and risk configuration, see
[ZeroClaw setup](zeroclaw-setup.md) and
[ZeroClaw security](zeroclaw-security.md).

## Bounded reads and reviewed maintenance

In-process reads use a 30-second cooperative deadline and return the stable
`read_timeout` error code on expiry. Narrow the query and retry; the server
releases its lock and remains available. Filesystem calls cannot be interrupted
until they return; use `--subprocess-reads` for a hard process timeout.
Mutations retain their existing isolated 30-second subprocess boundary.

`search` accepts `explain: true` for bounded excerpts with source provenance;
see [retrieval evaluation](../retrieval-evaluation.md). `find_candidates`
suggests potential duplicates without bodies; `merge_entries` previews before
applying a revision-bound plan. See [knowledge maintenance](../knowledge-maintenance.md).

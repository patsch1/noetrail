# Setting up ZeroClaw

This guide connects a ZeroClaw agent to a Noetrail vault without granting it
general shell, file, or web permissions.

ZeroClaw is one possible host. Noetrail's primary agent boundary is the generic
stdio MCP server described in [MCP clients](mcp-clients.md); nothing in the core
data format depends on ZeroClaw.

## Prerequisites

- a current ZeroClaw installation;
- Python 3.11 or newer in the ZeroClaw container;
- a data volume for `vault/` and `trash/`;
- a separate backup of that volume;
- a configured ZeroClaw model provider;
- for image understanding, a provider and model with vision support;
- a knowledge agent and a second agent alias `bookmark_fetcher` without its own
  channel.

The verified ZeroClaw version and known runtime limits are listed in
[ZeroClaw security](zeroclaw-security.md).

## 1. Provide the release content

The ZeroClaw process needs this read-only service root:

```text
/srv/noetrail/
├── .knowledge/                 read-only from the Git release
├── src/                        read-only from the Git release
├── tools/                      read-only from the Git release
├── vault/                      read-write from the data volume
└── trash/                      read-write from the data volume
```

Incoming chat files live outside this root, in the agent-specific ZeroClaw
workspace inbox: `discord_files/` for Discord, `matrix_files/` for Matrix.
The Noetrail MCP server receives that single
folder as a fixed `--attachment-inbox`; it must never receive a parent
workspace path or another agent's inbox. The inbox is temporary and does not
replace `vault/attachments/` on the data volume.

The Git release contains no vault data. In a Kubernetes deployment, `vault/`
and `trash/` are overlaid by volume mounts. The container runs as a non-root
UID that may write to those two mounts.

The production agent instructions live canonically in
[`deploy/zeroclaw/AGENTS.md`](../../deploy/zeroclaw/AGENTS.md). The release
process copies that file read-only into the knowledge agent's workspace. The
general `AGENTS.md` in the repository root is reserved for development and
maintenance agents and must not be published as a runtime prompt.

The always-loaded knowledge prompt also governs read-only retrieval. For a focused
question it allows one initial search and up to three distinct variants (key noun,
spelling correction or translation), stopping when source content is sufficient.
User-supplied filters remain authoritative; inferred type or status restrictions
must not hide ordinary notes. An unsuccessful bounded search is reported as a
search limitation, not proof that the entire vault lacks the information. These
limits also apply to extra claims about missing products, steps or values in an
otherwise successful answer. When a wrong-type diagnostic reveals candidates
outside an inferred type restriction, the agent reads their full contents before
answering, rather than stopping at the count of matching notes. Delegating hosts
must preserve the search limitation and source scope when restating the answer.
Aggregate questions stay inventory-only even when a delegator invents a first-page
request; title listings require the user's request for named entries.
The instructions do not depend on loading a capture skill and do not authorize saving
query variants as aliases. Historical answers use relationship validity rather
than an entry's creation date.

In a separate pod or container, the fetcher receives only the bookmark-fetcher
module as a read-only release file. Neither `.knowledge/`, `vault/`, `trash/`,
secrets, nor service-account tokens are mounted into that container. The
example
[NetworkPolicy](../../deploy/zeroclaw/bookmark-fetcher-network-policy.example.yaml)
allows only DNS and public HTTP(S); namespace, labels, and the DNS selector
must be adjusted before applying it.

Build the image from the repository root with
[`Containerfile.bookmark-fetcher`](../../deploy/zeroclaw/Containerfile.bookmark-fetcher),
then adapt
[`bookmark-fetcher-deployment.example.yaml`](../../deploy/zeroclaw/bookmark-fetcher-deployment.example.yaml)
and the NetworkPolicy to your environment. The pod starts the dependency-free
server with `--transport http`; `stdio` is for local testing only.

The secret store provides `BOOKMARK_FETCH_TOKEN` in the fetcher pod. The same
value is stored as `Authorization: Bearer ...` only in ZeroClaw's encrypted
live configuration for the `bookmark_fetch` MCP server. Neither the secret nor
the header value belongs in this repository. The readiness probe `/healthz`
needs no authentication; `/mcp` does.

Do not put the following into the container or service root:

- `.git/`;
- `imports/`;
- tests and local caches;
- Kubernetes service-account tokens, unless strictly required;
- backup or provider credentials.

### Migrate the schema before rolling out

A release with a higher `current_schema_version` must not write to the same
data volume alongside an older knowledge agent. Before the rollout, stop the
knowledge agent or scale it to zero, take a restorable volume snapshot, and run
the preview with exactly the new release:

```sh
noetrail --root /srv/noetrail migrate
```

Review the candidates and migration steps, then apply and validate:

```sh
noetrail --root /srv/noetrail migrate --apply
noetrail --root /srv/noetrail validate
```

Only then start the new agent. `migrate` is deliberately not offered over MCP.

## 2. Install the skill bundle

Copy the following directories read-only to
`<zeroclaw-install>/shared/skills/knowledge-vault/`:

```text
capture-knowledge/
manage-trash/
review-knowledge/
save-bookmark/
```

Additionally include `fetch-bookmark-metadata/` in its own read-only bundle for
the `bookmark_fetcher` agent. That agent receives none of the knowledge skills.

`import-knowledge` stays a local maintainer function and is not bound to the
production agent.

## 3. Apply the configuration

Copy the relevant sections from
[`deploy/zeroclaw/security-profiles.example.toml`](../../deploy/zeroclaw/security-profiles.example.toml)
into your live configuration, adjusting absolute paths and the agent alias to
your deployment.

Extend the existing agent:

```toml
[agents.knowledge]
# keep model_provider, runtime_profile, and channel bindings
risk_profile = "knowledge_vault"
skill_bundles = ["knowledge_vault"]
mcp_bundles = ["knowledge_vault"]
delegate_same_risk_profile = false
delegates = [{ agent = "bookmark_fetcher", mode = "independent" }]
acp_enable_mcp = true # only when used over ACP

[agents.bookmark_fetcher]
# reference the same or a smaller existing model provider
model_provider = "REPLACE"
risk_profile = "bookmark_fetcher"
skill_bundles = ["bookmark_fetcher"]
mcp_bundles = ["bookmark_fetcher"]
channels = []
```

The independent delegation is deliberately narrow: the knowledge agent may
reach only the explicit fetcher. The fetcher owns no confirmation-gated tools
and must not be attached to a user channel. ZeroClaw's global `link_enricher`
stays disabled, because it would place web text directly in front of an
incoming knowledge message.

The live configuration and all secret values stay outside this repository.

Set the authorization header for `bookmark_fetch` through ZeroClaw's masked
configuration interface. The repository template deliberately contains no
placeholder value:

```sh
zeroclaw config set mcp.servers.bookmark_fetch.headers
```

In the live configuration of the local `knowledge` MCP server, also set the
absolute path of the knowledge agent's channel-specific inbox. For Discord:

```text
--attachment-inbox /path/to/agents/knowledge/workspace/discord_files
```

The exact workspace path depends on the ZeroClaw installation and has to be
checked in the running container. This change belongs in the external
deployment configuration; the inbox and its contents belong neither in Git nor
on the knowledge data volume. The deployment profile must also auto-approve
`knowledge__list_pending_attachments` as a narrow read operation, and classify
`knowledge__add_attachment` as a confirmation-gated mutation like the other
writing knowledge tools.

`knowledge__refresh_bookmark` is a revision-checked, fill-only mutation for
existing bookmarks. Keep it in neither `auto_approve` nor `always_ask`: normal
supervised approval allows Discord's "Allow this session" to cover a requested
batch, while `always_ask` overrides that session grant. Pass the complete
isolated fetcher reply as `envelope`; never use `update` to strip the origin
of page-derived values.

### Stored-image delivery: Discord and Matrix

Use an outbox below the same agent workspace that the channel can read.
For **Discord on ZeroClaw v0.8.4**, add these MCP arguments:

```text
--attachment-outbox /path/to/agents/knowledge/workspace/knowledge_media
--attachment-delivery-marker-template "[PHOTO:{path}]"
```

Do **not** set `--attachment-delivery-marker-root` for Discord. This produces
an absolute `[PHOTO:/path/to/.../image.png]` marker inside the agent workspace.
Discord rejects relative targets with `marker target is not absolute`.
An absolute `[IMAGE:...]` instead enters ZeroClaw's image-input processing and
can disappear before the final reply. Discord's sender accepts `PHOTO` as an
alias for `IMAGE`, while the image-input parser recognizes only `[IMAGE:`.
The agent must copy `delivery_marker` exactly, without Markdown code fencing.
This contract is verified against the v0.8.4
[Discord marker parser](https://github.com/zeroclaw-labs/zeroclaw/blob/v0.8.4/crates/zeroclaw-channels/src/discord/markers.rs)
and [multimodal parser](https://github.com/zeroclaw-labs/zeroclaw/blob/v0.8.4/crates/zeroclaw-providers/src/multimodal.rs).

For **Matrix**, use its `matrix_files` inbox and a workspace-relative marker:

```text
--attachment-inbox /path/to/agents/knowledge/workspace/matrix_files
--attachment-outbox /path/to/agents/knowledge/workspace/knowledge_media
--attachment-delivery-marker-template "[IMAGE:{path}]"
--attachment-delivery-marker-root /path/to/agents/knowledge/workspace
```

The root verifies that the outbox is inside the workspace and emits a relative
target such as `[IMAGE:knowledge_media/image.png]`. That relative form belongs
to Matrix and must not be reused for Discord. The inbox names are channel
runtime directories, not vault paths. Check these adapter behaviors again when
upgrading ZeroClaw.

## 4. Check the configuration

In the deployment:

```sh
zeroclaw config schema
zeroclaw config list
zeroclaw doctor
zeroclaw skills list --agent knowledge
zeroclaw skills list --agent bookmark_fetcher
```

Before the build, in the Git checkout:

```sh
make check
```

In the deployed container, against the mounted volume:

```sh
noetrail --root /srv/noetrail validate
python3 tools/check_secrets.py /srv/noetrail
```

Restart the affected ZeroClaw session after changing MCP or skill bundles.

## 5. Verify the MCP connection

The agent must see tools prefixed with `knowledge__`, including:

- `knowledge__search`
- `knowledge__retrieve`
- `knowledge__inventory`
- `knowledge__list_views`
- `knowledge__run_view`
- `knowledge__get_entry`
- `knowledge__capture`
- `knowledge__list_pending_attachments`
- `knowledge__add_attachment`
- `knowledge__save_bookmark`
- `knowledge__refresh_bookmark`
- `knowledge__save_recipe`
- `knowledge__review_queue`
- `knowledge__trash`
- `knowledge__restore`
- `knowledge__validate`

`purge`, `migrate`, shell access, and arbitrary path operations must be absent.
`knowledge__list_pending_attachments` returns only short-lived tokens and
neutral metadata for recently received images, never paths.
`knowledge__add_attachment` is the single narrow path exception: it accepts a
channel-supplied source path or such a token, but can read only from the inbox
root fixed at server start. The fetcher agent may see only
`bookmark_fetch__fetch` and the tools needed to load its skill; it must own no
`knowledge__*` tools.

## 6. Smoke test

1. Ask the agent: "Show me my review list."
2. Have it store a synthetic thought.
3. Confirm the new Markdown file is on the data volume under `vault/`.
4. Have it run `knowledge__validate`.
5. Explicitly have it trash and restore the test entry.
6. Have it save a synthetic bookmark and confirm the knowledge agent delegates
   the URL to `bookmark_fetcher`.
7. Confirm the result contains no page or body, and that an attempt against
   `http://169.254.169.254/` returns status `blocked`.
8. Confirm nothing shows up as a change in the Git checkout:

   ```sh
   git status --short
   git ls-files vault trash
   ```
9. Send a synthetic PNG through the chat channel. If the agent sees no image
   path, it must obtain a token through
   `knowledge__list_pending_attachments`. Attach the image to a synthetic entry
   and confirm the blob is under `vault/attachments/`.
10. Confirm the pending list contains no inbox path, and that a token cannot be
    reused after a successful attach.
11. Offer a path outside the configured channel inbox, a symlink, an SVG, a
    file above 20 MiB, and a file modified after the token was issued through
    `knowledge__add_attachment`; every attempt must be rejected without any
    vault change.
12. Capture a synthetic product and a place, then create a `tasting` experience
    with `involves` and `took_place_at` relations and attach a photo directly to
    that experience.
13. Find the experience again through `knowledge__search` using
    `experience_kind`, `related_id`, `relation_predicate`, and a time range.
    Then explicitly trash all three synthetic entries.
14. Save a synthetic recipe link through `knowledge__save_recipe`, search for it
    with `interest_status = planned`, and connect a `cooking` experience through
    `involves`. Confirm the source description stays separate from your own
    recipe text and that no page body was copied.
15. Explicitly trash the recipe and the cooking experience.

## Web bookmark and recipe-link workflow

The knowledge agent owns no web tools. It delegates only the exact URL to the
`bookmark_fetcher` agent, whose only network tool comes from the separate HTTP
MCP pod. That server checks the initial destination and every redirect against
public IP addresses, accepts only the standard ports, ignores environment
proxies, limits runtime and response size, and reads only allowlisted HTML
metadata:

- URL and canonical URL;
- title, site, authors, date, and language;
- page description;
- fetch status.

The page body, scripts, headers, cookies, and credentials are never returned.
Obviously prompt-like meta fields are discarded. The NetworkPolicy is still
required, to catch DNS rebinding and application-level check failures at the
infrastructure layer. If the fetcher is unavailable, the knowledge agent stores
only the URL with `fetch_status = "not_attempted"`. For a recipe link, the same
neutral fields flow into `knowledge__save_recipe`; ingredients and instructions
are not extracted from the page. The agent creates an additional bookmark only
on explicit request.

## Concurrent changes

`knowledge__search`, `knowledge__get_entry`, and the review, relations, and
trash read operations return a `sha256:...` revision. Every MCP mutation of an
existing entry requires `expected_revision`. On a conflict the agent must
reread the entry and assess the intervening change; retrying blindly is
forbidden.

The CLI additionally holds a shared read/write lock under `vault/.locks/`. All
production writers must use the CLI or the Noetrail MCP. For multiple pods on
one shared-write volume, the storage driver must support cross-node `flock`
reliably; otherwise a single-writer deployment or an external lease is
required.

## Troubleshooting

### MCP tools are missing

- check `mcp.enabled` and the `knowledge_vault` bundle;
- check `mcp_bundles = ["knowledge_vault"]` on the right agent;
- set `acp_enable_mcp = true` when using ACP;
- restart the session after changes;
- check the absolute paths to `/usr/bin/env`, Python, and the MCP entry point;
- for the fetcher, additionally check its module path and bundle.

### Writes fail

- check volume mounts and ownership;
- make sure `.knowledge/`, `src/`, and `tools/` are readable;
- make sure only `vault/` and `trash/` need to be writable;
- run `noetrail --root /srv/noetrail validate`.

### A skill is not loaded

- check the target path under
  `<zeroclaw-install>/shared/skills/knowledge-vault/`;
- compare the bundle include list against the directory names;
- run `zeroclaw skills list --agent knowledge`;
- check `zeroclaw skills list --agent bookmark_fetcher` separately;
- restart the session.

## Automated rollout candidates

The `Publish ZeroClaw candidate` workflow runs after a successful `CI` push
on `main`. It publishes the exact tested commit as a ConfigMap on the generated
`deploy/test` branch, under `deploy/zeroclaw/candidate`. Superseded CI runs and
pull requests cannot publish candidates. Do not edit that generated branch.
This is a deployment pointer, not a package release or version change.

A cluster may reconcile that path with Flux using read-only repository access.
The reference integration first restarts an isolated ZeroClaw test deployment
with synthetic data. Direct and bounded-delegated model requests must produce
stored evidence; process readiness alone is not enough. A separate controller
may promote the exact tested revision only after a ready test Pod reports both
successful checks and the same schema version as production. No model process
needs Kubernetes write credentials.

Failed model requests, missing authentication and schema changes block
promotion. A schema change still requires the backup, migration and validation
procedure above; ordinary code rollout must never implicitly migrate a vault.
Configure the infrastructure's production pointer, pause/rollback procedure,
RBAC and smoke-test retry policy before activating this workflow in a cluster.
The GitHub workflow itself has no cluster credentials and cannot restart Pods.

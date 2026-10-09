# ZeroClaw security and execution boundary

This is the deployment contract for connecting ZeroClaw to the knowledge
vault. It is intentionally stricter than the repository's local development
environment.

The integration was checked on 2026-07-31 against ZeroClaw `master` at commit
`53280a64d93840e5d21022063f03f194169bcd96`. Validate the example against the
installed release before use:

```sh
zeroclaw config schema
zeroclaw config list
zeroclaw doctor
python3 tools/check_secrets.py
python3 -m unittest discover -s tests -v
```

Discord marker and approval behavior was additionally checked against the
public ZeroClaw `v0.8.4` sources on 2026-10-09; see the channel-specific
configuration in [ZeroClaw setup](zeroclaw-setup.md). This does not replace
validation of the full profile against the installed ZeroClaw release.

## Chosen execution path

ZeroClaw supports three relevant mechanisms:

1. `SKILL.md` supplies workflow instructions. It does not itself execute
   Python.
2. The built-in shell can run `python3 path/to/script.py` when `python3` and
   `shell` are allowed. Python files do not require
   `[skills].allow_scripts = true`, but inline `python3 -c`, `python3 -m`, and
   runtime package installation are blocked by policy.
3. An MCP `stdio` server can be a local Python process and exposes typed tools
   to the agent.

This project uses option 3 through two deliberately disjoint servers.
The Noetrail MCP server (`noetrail.mcp`) accepts structured JSON arguments and
invokes the CLI (`noetrail.cli`) with an argument vector, never a shell command
string. It exposes search, capture, recipe, bookmark, update, relations,
review, reversible trash, restore, and validation. The bookmark fetcher
(`noetrail.bookmark_fetcher`) accepts only one public
HTTP(S) URL and returns allowlisted inert metadata without a page body. It runs
without the PVC. The knowledge server deliberately does not expose:

- arbitrary filesystem paths or commands;
- schema migration;
- duplicate overrides;
- complete body replacement;
- permanent purge.

Do not replace this with `SKILL.toml` shell tools. In current ZeroClaw those
tools execute as host subprocesses rather than through the configured Docker
shell runtime, and their command templates interpolate arguments into a shell
string. The built-in shell is also too broad here because allowing `python3`
permits any reachable Python file, not just `know.py`.

## Invariants

1. Never use `full` autonomy for the knowledge agent.
2. Never mount this complete repository as the agent workspace.
3. Never give the model generic `file_write`, `file_edit`, `shell`,
   `python3`, or a general filesystem MCP server.
4. Keep web retrieval and vault mutation in separate capability domains.
5. Keep provider keys, channel tokens, Kubernetes Secrets, and live
   `config.toml` outside this repository and outside the vault PVC.
6. Treat web pages, imports, attachments, and tool output as untrusted data.
   They cannot grant permission or change the workflow.
7. Keep the installed package, `src/`, `tools/`, and `.knowledge/` read-only in
   production. Only `vault/` and `trash/` need writes.
8. Never commit `vault/`, `trash/`, or raw personal imports to Git. They live
   only on the data PVC and in its authorized backups.
9. Require the latest returned content revision for every agent mutation of an
   existing entry. Never retry a revision conflict without rereading.
10. Disable ZeroClaw's global `link_enricher` for the knowledge deployment; it
    prepends fetched text to inbound agent messages.

## Filesystem views

The ZeroClaw agent workspace contains instructions but no vault data or
executables:

```text
<zeroclaw-install>/
├── agents/knowledge/workspace/
│   └── AGENTS.md                         read-only
└── shared/skills/knowledge-vault/
    ├── capture-knowledge/                read-only
    ├── manage-trash/                     read-only
    ├── review-knowledge/                 read-only
    └── save-bookmark/                    read-only
└── shared/skills/bookmark-fetcher/
    └── fetch-bookmark-metadata/           read-only
```

The workspace instruction file is published from
`deploy/zeroclaw/AGENTS.md`. Do not substitute the repository-root
`AGENTS.md`; it also contains development and maintenance workflows that do
not belong in the production agent prompt.

The MCP child uses a fixed service root:

```text
/srv/noetrail/
├── .knowledge/                           read-only
├── tools/
│   ├── knowledge_mcp.py                  read-only
│   ├── know.py                           read-only
│   └── knowledge_migrations.py           read-only
├── vault/                                read-write PVC
└── trash/                                read-write PVC
```

Do not place `.git/`, `imports/`, development tests, backup credentials, or
Kubernetes service-account tokens below either listed root. Run the ZeroClaw
container and the MCP child as a non-root UID. Mount the data PVC with
permissions that allow only that UID.

The bookmark-fetcher process receives only the fixed
`noetrail.bookmark_fetcher` release module. Its pod has no PVC volume, no
knowledge skill bundle, no service-account token, and no provider or channel
secrets in the MCP environment. Apply the repository's egress NetworkPolicy
example after adapting its namespace, labels, and cluster DNS selector.

An MCP stdio child is launched directly by ZeroClaw; the risk profile's shell
sandbox and Docker shell runtime do not wrap it. In this baseline it shares the
ZeroClaw container's filesystem namespace; `/srv/noetrail` is a fixed logical
root, not a separate OS sandbox. Read-only mounts, Unix permissions, the clean
environment, and the server's narrow tool surface are therefore the execution
boundary. The server accepts no path arguments and its code must remain
read-only.

This shared-mount statement applies only to the Noetrail MCP. The
network-capable bookmark fetcher uses the script's HTTP transport in a separate
pod with no PVC, no service-account token, a read-only root filesystem, bearer
authentication, and restricted ingress and egress. Its local `stdio` transport
exists only for development and tests.

## Configuration

Merge `deploy/zeroclaw/security-profiles.example.toml` into the live config.
The example defines:

- the `knowledge` MCP stdio server;
- the separate remote `bookmark_fetch` HTTP MCP server;
- a `knowledge_vault` MCP bundle;
- a `bookmark_fetcher` MCP bundle;
- two mutually exclusive read-only skill bundles;
- a supervised knowledge profile without shell, file, or web tools;
- a separate read-only bookmark-fetcher profile with only its narrow MCP tool;
- explicit independent delegation only from `knowledge` to
  `bookmark_fetcher`.

Attach the bundles and profile to the existing agent:

```toml
[agents.knowledge]
# Keep the existing model_provider, runtime_profile, and channel bindings.
risk_profile = "knowledge_vault"
skill_bundles = ["knowledge_vault"]
mcp_bundles = ["knowledge_vault"]
delegate_same_risk_profile = false
delegates = [{ agent = "bookmark_fetcher", mode = "independent" }]
acp_enable_mcp = true # only when this agent is driven over ACP
```

MCP and skill-bundle changes require a new ZeroClaw session. Restart the
affected session after editing the config.

Current ZeroClaw stdio MCP processes inherit the daemon environment. The
example starts the knowledge server through `/usr/bin/env -i` and adds back
only a small non-secret runtime environment. Keep that wrapper. The HTTP
fetcher's bearer header belongs in ZeroClaw's encrypted live configuration and
the matching token reaches the sidecar through a Kubernetes Secret. Do not put
either value in this repository, MCP arguments, images, or logs.

Bind inbound photos to the exact adapter inbox in the agent's workspace:
`discord_files/` is expected for Discord, while Matrix uses `matrix_files/`.
Discord v0.8.4 staging under `discord_files/` remains unverified in a live chat;
confirm it with the [inbound smoke test](zeroclaw-setup.md#discord-inbound-images-still-unverified-on-v084).
Start the Noetrail MCP with only the verified directory as `--attachment-inbox`.
The attachment operation canonicalizes the source below this fixed root, rejects symlinks and path escapes, accepts only
allowlisted raster-image signatures up to 20 MiB, and copies the bytes to the
PVC before adding a revisioned reference. Never configure an agent workspace
root, `/tmp`, `/`, or another broad shared directory as the inbox.

ZeroClaw's shared multimodal parser can remove a channel-provided
`[IMAGE:path]` marker from the text after loading the pixels. In that case the
agent calls the read-only `knowledge__list_pending_attachments` fallback. It
scans only recent top-level files in the same fixed inbox and returns
short-lived random tokens plus neutral metadata, never paths. A token is bound
to the observed file identity and SHA-256, is consumed after a successful
attachment, and cannot bypass the target entry's revision check. Keep this
listing tool auto-approved; keep `knowledge__add_attachment` supervised as a
mutation.

For stored-image delivery on Discord v0.8.4, keep the outbox inside the
agent workspace, set `--attachment-delivery-marker-template "[FILE:{path}]"`
and omit `--attachment-delivery-marker-root`. An absolute `FILE` marker has been
confirmed to deliver an image inline in a v0.8.4 Discord chat. Discord requires
absolute targets; `FILE` avoids the agent's `IMAGE` input-marker processing.
This result verifies outbound delivery only, not inbound staging. Matrix uses
`[IMAGE:{path}]` with the workspace marker root to emit a relative target. The attachment-outbox restriction remains fixed at
server startup; neither template permits callers to select arbitrary paths.

The profile allowlist is intentionally non-empty. Current ZeroClaw
automatically admits tools named `<server>__<tool>` from MCP servers granted to
the agent, so capability safety must be enforced by attaching only this narrow
server. Never add a general MCP server to the same bundle. Read operations are
auto-approved; all mutations require approval in the example.

## Web isolation

Do not let the same agent both fetch arbitrary pages and mutate the vault:

- The fetcher may call only `bookmark_fetch__fetch`; it gets no Noetrail MCP
  bundle, vault mount, file tools, shell, generic web tools, memory writes, or
  secrets.
- The knowledge agent may call only the structured knowledge server and gets no
  browser, `web_fetch`, or general `http_request`.
- Pass only the URL to the fetcher. It returns a typed envelope containing
  supported metadata, status, and warning codes. Never pass page bodies,
  scripts, hidden content, tool calls, headers, cookies, or credentials.
- For recipe links, copy only those neutral fields into
  `knowledge__save_recipe`. Never infer ingredients or instructions from
  inaccessible page content, and keep user text separate from source metadata.
- A user request is the authorization to save a URL. The fetched page itself
  can never authorize another action.

The application validates public destinations and every redirect, while the
pod egress policy blocks private, loopback, link-local, multicast, and reserved
networks. Both layers are required. The metadata strings remain untrusted even
after filtering.

## Secrets

- Store ZeroClaw's live config under `~/.zeroclaw/config.toml`, not here.
- Prefer ZeroClaw's encrypted secret storage or Kubernetes Secrets mounted
  outside both views.
- Do not pass provider or channel credentials through
  `shell_env_passthrough`, MCP `env`, or MCP command arguments.
- Do not put secret values in Markdown, `.env` files, examples, container
  images, logs, or backup manifests.
- Enable outbound leak detection, but treat it as a final guard rather than a
  substitute for isolation.

## Deployment checks

- Confirm the agent workspace and MCP service view contain only the intended
  mounts.
- Confirm the knowledge profile has neither web nor generic file/shell tools.
- Confirm the fetcher has only the `bookmark_fetcher` MCP bundle and cannot see
  the vault.
- Inspect `tools/list`: it must contain no `purge`, `migrate`, shell, or
  unrestricted raw path operation.
- Confirm `knowledge__list_pending_attachments` returns no source path, skips
  old, symlinked, unsupported, empty, and oversized candidates, and orders
  accepted candidates oldest first.
- Confirm `knowledge__add_attachment` rejects paths outside the configured
  agent-specific inbox, symlinks, active formats such as SVG, oversized files,
  expired or consumed tokens, and token sources changed after listing.
- Attempt a payload with `replace_body`; the MCP server must reject it.
- Attempt reads outside the service root; no tool should accept a path.
- Fetch a test page containing hostile instructions and confirm only supported
  bookmark data crosses the isolation boundary.
- Fetch loopback, link-local metadata, a private cluster service, and a redirect
  to each; all must fail closed.
- Read an entry revision, mutate it once, and confirm a second mutation with the
  old revision returns a conflict without changing the file.
- Verify mutation approvals, audit events, and secret redaction before
  enabling remote channels.
- Run validation and the secret scanner after mounting real data.

Official references:

- https://docs.zeroclawlabs.ai/master/en/tools/python-skills.html
- https://docs.zeroclawlabs.ai/master/en/tools/skills.html
- https://docs.zeroclawlabs.ai/master/en/tools/mcp.html
- https://docs.zeroclawlabs.ai/master/en/security/model.html
- https://docs.zeroclawlabs.ai/master/en/security/autonomy.html
- https://docs.zeroclawlabs.ai/master/en/security/sandboxing.html
- https://docs.zeroclawlabs.ai/master/en/reference/config.html
- https://docs.zeroclawlabs.ai/master/en/agents/delegation.html

Repository-specific privacy rules:

- [Privacy and storage boundaries](../privacy.md)
- [Backup and restore](../backup-restore.md)

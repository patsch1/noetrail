# Installation and lifecycle

Python 3.11 or newer is required. The runtime has no third-party Python
dependencies, so nothing below needs a container, an orchestrator, or a
service account. Pick the operating mode that matches where the vault should
live; the vault format and the agent boundary are identical in all three.

## Operating modes

| Mode | Install | Data lives | Use when |
| --- | --- | --- | --- |
| Single user, own machine | `uvx` / `pipx` / a virtual environment | `~/noetrail` | a desktop MCP client is the only consumer |
| Shared host or server | wheel into `/opt/noetrail-runtime` | `/srv/noetrail-data` | several processes or users, scheduled backups |
| Hardened container | image plus a manifest | a persistent volume | an always-on chat channel and a network policy |

The container deployment is one option among these, not a prerequisite. Its
reference manifests are [ZeroClaw setup](integrations/zeroclaw-setup.md) and
[ZeroClaw security](integrations/zeroclaw-security.md); the security
properties they rely on come from the CLI and MCP boundary described here and
in [Connecting an MCP client](integrations/mcp-clients.md).

## Install: single user, own machine

The alpha is available from the
[GitHub prerelease](https://github.com/patsch1/noetrail/releases/tag/v0.10.0a1)
and [TestPyPI](https://test.pypi.org/project/noetrail/0.10.0a1/). Install it in a
fresh virtual environment:

<!-- docs-check: skip - installs from TestPyPI over the network -->

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://test.pypi.org/simple/ --no-deps noetrail==0.10.0a1
.venv/bin/noetrail quickstart
```

Production PyPI publication is still pending. After that approval, the default
package-index command will work:

<!-- docs-check: skip - uvx and pipx run install from a package index over the network -->

```sh
uvx noetrail quickstart          # or: pipx run noetrail quickstart
```

Today, install from a checkout as an alternative and run the same command:

<!-- docs-check: skip - installs from the network -->

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/noetrail quickstart
```

`quickstart` creates `~/noetrail/data` and `~/noetrail/config`, writes a few
sample entries, and prints the MCP server definition with absolute paths.
`--path` moves both roots elsewhere; `--json` prints the same information
machine-readably. It is idempotent and can be repeated.

## Install: shared host or server

Build or download a wheel and install it into a dedicated virtual environment:

```sh
python3 -m pip wheel . --wheel-dir dist
python3 -m venv /opt/noetrail-runtime
/opt/noetrail-runtime/bin/python -m pip install dist/noetrail-*.whl
```

The wheel installs three canonical commands:

- `noetrail` for local capture, search, validation, migration, and diagnosis;
- `noetrail-mcp` for the narrow vault-facing MCP server;
- `noetrail-bookmark-fetcher` for the separately isolated web-facing MCP
  server.

The former `knowledge`, `knowledge-mcp`, and `knowledge-bookmark-fetcher`
commands remain compatibility aliases. New integrations should use the
Noetrail names; existing ZeroClaw deployments can migrate deliberately.

It also installs the built-in specification, templates, schemas, and bundled
packs below the environment's read-only `share/noetrail/`
directory. Program resources never need to be copied into the private data
root.

## Initialize

`quickstart` does this for you. Use `init` when you want the empty instance
without sample entries, which is the normal choice on a server.

Choose distinct data and instance-configuration roots. `init` creates only
empty private runtime directories and an empty local-pack directory; it is
safe to repeat:

```sh
/opt/noetrail-runtime/bin/noetrail \
  --data-root /srv/noetrail-data \
  --config-root /etc/noetrail \
  init
```

The data root contains `vault/`, `trash/`, `imports/raw/`, and `imports/work/`.
Keep the entire root on private storage with appropriate backups. The config
root may contain a local `relation-types.yaml` and declarative packs under
`packs/`; never put secrets in either root.

Flags can be replaced with `NOETRAIL_DATA_ROOT`, `NOETRAIL_CONFIG_ROOT`, and
`NOETRAIL_BUILTINS_ROOT`. The former `KNOWLEDGE_*` variables remain supported
as lower-priority aliases. The built-ins variable is normally unnecessary for
an installed wheel. Existing source checkouts and deployments may continue to
use combined `--root` compatibility mode.

Verify the empty installation and then capture a synthetic note:

```sh
/opt/noetrail-runtime/bin/noetrail \
  --data-root /srv/noetrail-data \
  --config-root /etc/noetrail \
  doctor

/opt/noetrail-runtime/bin/noetrail \
  --data-root /srv/noetrail-data \
  --config-root /etc/noetrail \
  capture --type note --title "First note" --text "The installation works."
```

`doctor` reports roots, permission failures, invalid packs, and entry schema
version counts. It deliberately does not return entry titles or bodies.

## Upgrade

1. Create and verify a data-root snapshot or backup.
2. Run `noetrail doctor` and `noetrail validate` with the current version.
3. Install the new wheel into a new virtual environment rather than replacing
   the known-good environment in place.
4. Run the new environment's `noetrail migrate`, which previews without
   writing. Review every
   reported structural change.
5. Point the local process or deployment at the new environment and run
   `noetrail doctor` plus `noetrail validate` again.
6. Apply `noetrail migrate` only when the migration is explicitly approved,
   then validate once more.

Installing schema 12 does not implicitly migrate schema-8, schema-9,
schema-10, or schema-11 entries. They remain readable and editable during the current
compatibility window. A schema-9 entry simply records no provenance, which
reads as "nothing was recorded", not as "these values are trusted";
`migrate --apply` is what fills in what can still be inferred. The 10-to-11
step only makes relation validity legal — it adds no interval to any existing
relation. The 11-to-12 step makes entry aliases legal but invents none. An
entry therefore looks identical after either step apart from its version.

## Roll back

Before an on-disk migration, roll back by selecting the previous virtual
environment or immutable image. After an on-disk migration, do not manually
lower `schema_version` or move fields: restore the matching pre-migration data
snapshot and the previous program version together. Attachments and trash are
part of that snapshot boundary.

## Uninstall

Stop MCP processes and uninstall or remove only the program environment:

```sh
/opt/noetrail-runtime/bin/python -m pip uninstall noetrail
```

Uninstall deliberately leaves data and instance configuration untouched.
Delete either only as a separate, explicitly reviewed data-retention action;
existing backups may retain independent copies until their own expiry.

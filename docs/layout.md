# Noetrail application, configuration, and data layout

## Purpose

Noetrail separates immutable program resources, instance configuration, and
private data. This lets an installed CLI or MCP server operate on a vault in a
clean data directory without copying source code or built-in schemas into that
directory.

## Roots

| Root | Contents | Expected access |
| --- | --- | --- |
| application root | installed Python code and launchers | read-only |
| built-ins root | `SPEC.md`, core schemas, relation defaults, templates, and bundled packs | read-only |
| instance config root | local relation override and custom schema packs | read-only during normal agent operation |
| data root | `vault/`, `trash/`, `imports/`, attachments, locks, and derived runtime data | private; writable where required |

The application root is derived from the installed program. The other roots
can be supplied explicitly:

```sh
noetrail \
  --data-root /srv/noetrail-data \
  --config-root /etc/noetrail \
  --builtins-root /opt/noetrail/share/noetrail/.knowledge \
  validate
```

The MCP server accepts the same root options and forwards the fully resolved
layout to every CLI child process:

```sh
noetrail-mcp \
  --data-root /srv/noetrail-data \
  --config-root /etc/noetrail \
  --builtins-root /opt/noetrail/share/noetrail/.knowledge
```

Equivalent environment variables are available for local process management:

- `NOETRAIL_DATA_ROOT`
- `NOETRAIL_CONFIG_ROOT`
- `NOETRAIL_BUILTINS_ROOT`

The former `KNOWLEDGE_*` names remain lower-priority compatibility aliases.
Command-line values take precedence over environment variables. A separated layout
always requires a data root. Without an explicit config root, the application
uses `$XDG_CONFIG_HOME/noetrail` or `~/.config/noetrail`. If that directory is
absent but an existing `knowledge` directory is present, Noetrail continues to
use the legacy directory. Without an explicit built-ins root, a source checkout
uses its `.knowledge/` directory and an installed wheel uses its immutable
`share/noetrail/.knowledge/` resources. The previous installed resource path
is recognized as a compatibility fallback.

## Compatibility mode

The existing combined layout remains supported:

```sh
noetrail --root /srv/noetrail validate
```

In that mode:

```text
/srv/noetrail/
├── .knowledge/       built-ins and current instance configuration
├── vault/            private data
└── trash/            private data
```

`--root` cannot be combined with separated-root options. When no root option or
layout environment variable is present, the CLI continues to discover a
combined root from the current directory or source checkout. This preserves
the existing development and ZeroClaw deployment contract while migration is
planned separately.

## Instance configuration

The instance config root may contain an optional `relation-types.yaml`. When
absent, the built-in relation definitions are used. Local declarative schema
packs live in `packs/<pack-id>/pack.yaml`; bundled packs use the equivalent
directory below the built-ins root. Their format and trust boundary are
documented under [Declarative schema packs](schema-packs.md).

An optional `views.yaml` contains named, declarative saved searches. It is
validated as bounded data and cannot execute code or mutate the vault. Its
format is documented under [Declarative saved views](saved-views.md).

Configuration children must remain directly below the resolved config root.
The runtime rejects a `packs/` directory, relation override, or saved-view file
implemented as a symbolic link. Pack directories, manifests, template paths,
and saved views are validated before use. Packs cannot supply their own storage
paths; custom entry paths are derived from validated pack and type identifiers.

## Deployment boundary

For the target Kubernetes layout:

- application code and built-ins come from the immutable image or release;
- instance configuration comes from a read-only deployment-specific mount;
- only the data root is backed by the private data PVC;
- the attachment inbox remains a separate narrow, temporary channel mount;
- the isolated bookmark fetcher receives none of these roots except its own
  fixed executable resource.

The repository's current ZeroClaw example still uses combined compatibility
mode. Changing the live mounts and CLI arguments is a deployment migration and
must be coordinated with the separate infrastructure project after the
separated layout has completed its repository-level validation.

# Versioning and compatibility policy

The project uses Semantic Versioning for program releases and maintains two
independent data versions:

- `schema_version` for the stable vault core envelope;
- `type_version` for one registry type definition.

Before `1.0`, a minor program release may intentionally change CLI or MCP
behavior, but on-disk changes still require an ordered dry-run migration and a
documented rollback path. Patch releases must not require a core data
migration. A type-pack version change does not silently rewrite entries.

Public pre-releases use PEP 440-compatible identifiers in Python artifacts and
matching Git tags and release notes. The first published public alpha is
`0.10.0a1` with tag `v0.10.0a1`. It preserves the existing internal
`0.8.x`/`0.9.x` lineage and is not a promise of public API stability.

`0.10.0a2` updates public documentation and synthetic pilot materials without
changing runtime behavior or the core schema.

`0.10.0a3` adds labelled title/alias typo candidates and caller-supplied
retrieval variants. Core schema 12 and stored entries remain unchanged.

The compatibility window for a previous core schema is stated in the release
notes. During such a window, ordinary capture or update operations must not
implicitly migrate old entries. Once support is removed, `noetrail doctor`
must detect the old version before a mutation is attempted.

Release artifacts must be built from a clean tagged checkout. CI and release
jobs may use only source files and synthetic fixtures; no real vault, PVC,
attachment inbox, raw import, backup, or secret is part of a build.

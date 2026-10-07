# Public product decision record

This document retains the product decisions that are useful to contributors
without publishing an operator's private rollout journal. It contains no real
vault measurements, deployment revisions, backup state, user content, or
infrastructure inventory.

Operational rollout notes belong in the private infrastructure repository or
another access-controlled system. They must not be copied into issues, pull
requests, test output, release artifacts, or this source distribution. Before
making an existing repository public, run `make publication-check`: deleting
sensitive material in a later commit does not remove it from Git history.

Forward-looking work lives in [ROADMAP.md](../../ROADMAP.md). Detailed design
choices live in the [architecture decision records](../adr/), and user-visible
changes live in [CHANGELOG.md](../../CHANGELOG.md). Those are the authoritative
records; this page is only a compact index of why the project took its current
shape.

## Accepted product decisions

### Local-first Markdown is the source of truth

Entries remain portable Markdown with YAML frontmatter. Indexes and vector
stores are rebuildable sidecars, never the only copy of user knowledge.

### Program, configuration, and private data are separate

Installed code and bundled schema packs may be read-only. Instance
configuration and the data root are explicit. Vaults, imports, trash,
attachments, and backups stay outside the source repository.

### Declarative schema packs extend the core

The stable core owns identity, revisions, lifecycle, relations, attachments,
provenance, migrations, and validation. Data-only schema packs describe domain
types without Python, shell, network instructions, or executable hooks.

### The CLI owns vault rules

The CLI is the single implementation of mutations and validation. MCP exposes
a narrow typed interface and translates mutations into CLI argument vectors;
it does not implement a second set of storage rules.

### MCP is the portable integration boundary

Noetrail's core does not depend on an agent host. ZeroClaw is a hardened
deployment profile, while desktop clients, editors, and other MCP hosts can use
the same server and on-disk format.

### Untrusted web content is isolated from vault writes

Bookmark enrichment runs in a separate service without vault access and
returns allowlisted metadata rather than page bodies. A knowledge-capable
agent must not receive a general-purpose web or shell tool through this path.

### Mutations are revision checked and recoverable

Updates require the revision returned by the last read. Whole-entry deletion
uses reversible trash. Permanent purge, schema migration, and restore remain
explicit maintenance operations rather than agent tools.

### AI authorship is disclosed, not implied

The repository was written by AI coding agents throughout, and a reader who
only sees `AGENTS.md` and the `Co-Authored-By` trailers has to infer that. It
is now stated in `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, and
[How this project was built](../ai-authorship.md). The alternative — letting
the trailers speak for themselves — was rejected: someone deciding whether to
run this against their private notes should not have to read a commit log to
find out how it was produced, and a project that is quiet about it invites the
suspicion that it was hiding it.

### Reviews start from a fetched `origin/main`

A review once ran against a checkout twelve merges behind and reported
already-shipped work as open, producing a pull request that could not merge.
`AGENTS.md` now requires `git fetch` and a comparison against `origin/main`
before reviewing, planning, or changing anything, because `git status` reports
a stale tree as clean.

### Releases are synthetic and reproducible enough to audit

CI uses generated data only. Release acceptance exercises an installed wheel,
schema upgrade and restore, source-distribution completeness, reproducibility,
checksums, and a dependency-derived SBOM before any publishing job can run.

## Publication boundary

The current tree is only one part of a publication review. `make
publication-check` also scans every reachable Git blob for credential patterns,
private-data paths, and the retired operational journal. If that history check
fails, publication requires either a deliberate history rewrite or a new clean
public repository. Both choices change provenance and are maintainer decisions;
the check never performs either automatically.

# Development process and AI authorship

Noetrail's code, tests, and documentation were written by AI coding agents
under one human maintainer's direction. This includes the core implementation,
not just generated helpers. The maintainer directs the work, reviews changes,
and remains responsible for releases and support.

This disclosure lets users assess the development process before trusting the
software with personal notes. Automated checks provide evidence about specific
properties; they are not a guarantee of correctness or a substitute for review.

## Public provenance

The public history begins with a reviewed source snapshot dated 2026-10-07.
Earlier development history remains in a private archive because it contained
operational notes and private context. Earlier `Co-Authored-By` trailers are therefore
not independently verifiable from the public repository.

[AGENTS.md](../AGENTS.md) documents repository instructions for coding agents.
[Contributing](../CONTRIBUTING.md) describes the same quality and privacy
requirements for all contributors. The [decision log](internal/decision-log.md)
records product decisions and their reasons.

## Required checks

Every change must pass six contributor gates:

| Gate | What it checks |
| --- | --- |
| `ruff` | style, unused code, and common defects |
| `mypy` | types and narrowing, with strict checks on selected boundary modules |
| `unittest` | behavior against synthetic fixtures |
| `tools/coverage.py` | 80% overall and 95% on the specified migration, layout, JSON-RPC, saved-view, and SSRF paths |
| `tools/check_secrets.py` | recognized credential patterns in tracked source |
| `tools/check_git_boundary.py` | private data paths and required Git exclusions |

CodeQL runs on public changes. Publication also requires a full-history audit,
release-consistency checks, source-distribution acceptance, and tests of a
freshly installed wheel. Release artifacts carry checksums and build provenance.
See [Release process](releasing.md).

Tests written by the same process as the implementation can share its mistakes.
Coverage measures executed statements, not correctness. Synthetic regression
cases, fixed expected results, migration-preservation checks, and independent
user feedback all contribute different evidence. The planned
[alpha pilot](alpha-pilot.md) has not yet produced participant results.

## Design boundaries

Markdown remains the source of truth. The Python runtime has no third-party
package dependencies, and schema packs cannot execute code. Updates support
revision checks, whole-entry deletion normally uses recoverable trash, and
migrations are explicit. Permanent purge and some local CLI maintenance
operations are intentionally outside the agent tool surface. A complete backup
is still required for recovery after a migration or a destructive operation.

AI clients are a separate trust boundary: a connected client can read vault
content and may send it to its model provider. See [Privacy](privacy.md).

## Accountability and reports

The maintainer owns release decisions, the response commitments in
[SECURITY.md](../SECURITY.md), and enforcement of the
[code of conduct](../CODE_OF_CONDUCT.md). AI authorship does not change how bugs
or vulnerabilities are handled. Use [Support](../SUPPORT.md) for reproducible
bugs and the private security channel for vulnerabilities.

The Apache-2.0 license and documented Markdown format allow inspection and
independent use of stored entries. The format is specified in
[.knowledge/SPEC.md](../.knowledge/SPEC.md).

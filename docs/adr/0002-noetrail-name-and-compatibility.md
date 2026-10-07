# ADR 0002: Noetrail name and compatibility surface

- Status: accepted
- Date: 2026-08-02

## Context

The working names `knowledge` and `personal-knowledge-vault` described the
repository but were too generic for an independently published project. The
project also already has a live ZeroClaw integration whose command aliases,
environment variables, MCP server key, and `knowledge__*` tool namespace must
not be changed accidentally as part of a branding decision.

A point-in-time availability screen found no exact general-web or GitHub
repository match for `Noetrail`, no package under that name on PyPI, npm, or
crates.io, and no RDAP registration record for `noetrail.com` or
`noetrail.org`. This screen is neither trademark clearance nor a reservation.

## Decision

The public project and Python distribution name is **Noetrail**. The name is a
coined combination of a noetic association with knowledge and a trail through
linked notes, memories, sources, and experiences.

The canonical installed surface is:

- Python distribution: `noetrail`;
- console commands: `noetrail`, `noetrail-mcp`, and
  `noetrail-bookmark-fetcher`;
- installed built-in resources: `share/noetrail/.knowledge/`;
- environment variables: `NOETRAIL_DATA_ROOT`, `NOETRAIL_CONFIG_ROOT`, and
  `NOETRAIL_BUILTINS_ROOT`;
- default instance configuration: `$XDG_CONFIG_HOME/noetrail` or
  `~/.config/noetrail`;
- MCP `serverInfo.name`: `noetrail` and `noetrail-bookmark-fetcher`.

The source repository uses Apache License 2.0. That license covers the
distributed project code and documentation, not private user vault contents
that are kept outside the distribution.

## Compatibility

The rename does not require a vault migration and does not rename the portable
`.knowledge/` format directory or stable `kn_...` entry IDs.

For existing installations:

- `knowledge`, `knowledge-mcp`, and `knowledge-bookmark-fetcher` remain console
  aliases;
- `KNOWLEDGE_*` root variables remain lower-priority aliases;
- an existing default `~/.config/knowledge` is used when the canonical
  Noetrail directory does not yet exist;
- the former installed built-ins path remains a read-only fallback;
- the ZeroClaw MCP server key and resulting `knowledge__*` tool namespace stay
  stable until a separately coordinated deployment migration.

These aliases should not be removed before a documented major-version decision.

## Consequences

New documentation and integrations use Noetrail names. Existing local and
ZeroClaw installations keep working while operators migrate on their own
schedule. The GitHub repository was renamed to `patsch1/noetrail` when this
decision was first published. Package-index publication and domain reservation
remain separate release actions.

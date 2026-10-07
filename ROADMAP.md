# Roadmap

Where Noetrail is going, for people deciding whether to use or contribute to
it. This is a one-person project before `1.0`, so this page lists direction and
ordering, not dates. Anything here can change; what has already been decided
and shipped is in [the decision log](docs/internal/decision-log.md).

## Where it stands

`0.10.0a2` is the current public alpha, available as a GitHub prerelease and on
PyPI and TestPyPI. It updates documentation and synthetic pilot materials; the
first public alpha was `0.10.0a1`. The vault format is at core
schema 12, the CLI and the Noetrail MCP server share one validated code path,
custom types are declarative schema packs, and the runtime has no third-party
dependencies. Migrations are deterministic and previewable, and every release
is checked by building it, running the suite from the unpacked source
distribution, and exercising an installed wheel.

Alpha means the CLI and MCP surfaces can still change within a minor release.
On-disk changes cannot: they need an ordered dry-run migration and a documented
rollback, before and after `1.0`. See
[versioning and compatibility](docs/versioning.md).

## Next

**Publication is complete.** The source is public, and the first GitHub
prerelease, PyPI and TestPyPI distributions are available. The workflow
publishes identical CI-built artifacts through Trusted Publishing from a
protected environment. Future uploads still require a maintainer decision. See
[Release process](docs/releasing.md).

CodeQL scans public changes through the repository workflow. A successful
scan is part of source acceptance; it does not approve a package upload.

**Drop the compatibility shims (`0.11.0`).** `tools/know.py`,
`tools/knowledge_mcp.py`, and `tools/bookmark_fetch_mcp.py` exist only for
deployments that invoke those paths directly. The `knowledge*` console aliases
and the `KNOWLEDGE_*` environment variables stay longer; removing them is a
major-version decision recorded in
[ADR 0002](docs/adr/0002-noetrail-name-and-compatibility.md).

**A cheaper index update.** Once an index exists, every write rewrites the
whole file: measured at about 2.4 s on a 50,000-entry vault. That is why the
index is opt-in rather than automatic, and it is the next thing worth fixing
for anyone running at that size.

**Everyday retrieval and a small alpha pilot.** The 100-question diagnostic
set now exposes lexical gaps, including German paraphrases, typos and language
changes. Use its category results and the three-to-five-person
[alpha pilot](docs/alpha-pilot.md) to select the next improvement. See
[retrieval evaluation](docs/retrieval-evaluation.md). In-process reads now have
cooperative deadlines; subprocess reads remain available for hard isolation.

**Toward `1.0`.** The gate is not a feature list. It is: the on-disk format
stable enough to promise compatibility for, a stated support window per
released line, and a deprecation policy that survives the first removal.

## Deliberately not planned

These are decisions, not gaps. Each would change what the project is.

- **A hosted multi-user service**, opaque cloud sync, or any storage the user
  does not control.
- **A graphical or mobile application.** Markdown files and an editor are the
  graphical interface.
- **Executable plugins or schema hooks.** Schema packs are data. This is the
  boundary that keeps an installed pack from being a code-execution vector.
- **Built-in vault encryption** beyond the documented filesystem and backup
  controls.
- **A bundled embedding provider.** Provider integrations would add runtime
  dependencies and require explicit decisions about how vault content is
  processed. Noetrail instead defines a vector sidecar that an external
  provider writes, and reranks lexical candidates against it; it computes
  no embeddings itself. See [Embeddings](docs/embeddings.md).
- **Semantic search as the source of truth.** Markdown stays authoritative, and
  a ranking that depends on a model version cannot be reproduced by a reader
  holding only the files.
- **A remote read-write Noetrail MCP exposed to the internet.**
- **Automatic installation of packs** suggested by webpages or vault content.

## Influencing it

Open an issue with the feature form. A proposal that names the problem, the
smallest change that solves it, and the expected tradeoffs helps the maintainer
evaluate scope and priority. [CONTRIBUTING.md](CONTRIBUTING.md) describes the
gates a change has to pass.

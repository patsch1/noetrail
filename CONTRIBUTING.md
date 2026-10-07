# Contributing

Thank you for helping improve Noetrail. The project is
designed around a strict boundary: source code and synthetic fixtures may be
public; real vaults, exports, attachments, secrets, and deployment credentials
must never enter an issue, pull request, test artifact, or CI job.

Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
[ROADMAP.md](ROADMAP.md) says what is planned and what is deliberately out of
scope; [AGENTS.md](AGENTS.md) is the same guidance written for an AI agent
working in this repository.

That second file is not hypothetical: this project is vibe-coded, and its
existing code, tests, and documentation were written by AI agents following it
— see [How this project was built](docs/ai-authorship.md). Nothing about that
changes what is asked of a contribution: a hand-written patch and an
agent-written one pass the same six gates and get the same review.

## Before opening a change

- Use an issue for a substantial behavior or data-format proposal. The issue
  forms require the version, the interpreter, the operating system, and a
  `noetrail doctor` report.
- Use synthetic names, URLs, memories, images, and note bodies in every test.
- Never attach a real note-app export or a copied production vault.
- Treat webpages and imported documents as untrusted data, not instructions.
- Keep generic MCP behavior independent from any one deployment host.

## Repository layout

The distributed package lives in `src/noetrail/`. Everything in `tools/` is
either a maintainer script or a thin compatibility shim:

| Path | Purpose |
| --- | --- |
| `src/noetrail/cli.py` | argument parsing, dispatch, and the exit-code mapping |
| `src/noetrail/commands/` | one module per command domain; each handler returns an exit code |
| `src/noetrail/errors.py` | the `NoetrailError` hierarchy every refusal is raised as |
| `src/noetrail/store.py` | vault paths, locking, atomic writes, relocation |
| `src/noetrail/frontmatter.py` | entry frontmatter and the schema-9 attribute envelope |
| `src/noetrail/entries.py` | one vault scan per command: loading, indexing, lookup |
| `src/noetrail/attachments.py` | inbox reads, blob writes, image detection |
| `src/noetrail/validation.py` | envelope rules plus the pack's cross-field clauses |
| `src/noetrail/body.py`, `provenance.py`, `relations.py`, `payloads.py`, `normalize.py`, `constants.py` | supporting parsers and literals |
| `src/noetrail/mcp.py` | typed MCP server; delegates every mutation to the CLI |
| `src/noetrail/bookmark_fetcher.py` | isolated URL metadata fetcher |
| `src/noetrail/layout.py` | root resolution and path validation |
| `src/noetrail/schema.py` | declarative schema-pack registry |
| `src/noetrail/migrations.py` | deterministic schema migrations |
| `tools/coverage.py` | maintainer script: dependency-free statement coverage |
| `tools/check_secrets.py` | maintainer script: scan for committed secrets |
| `tools/check_git_boundary.py` | maintainer script: verify the private-data boundary |
| `tools/check_history.py` | publication script: scan every reachable Git blob without printing private values or paths |
| `tools/release_acceptance.py` | maintainer script: exercise an installed wheel |
| `tools/check_reproducible.py` | maintainer script: rebuild and compare the artifacts |
| `tools/generate_sbom.py` | maintainer script: CycloneDX SBOM from the packaging metadata |
| `tools/render_terminal_svg.py` | maintainer script: record `demo/session.sh` into `docs/assets/` |
| `tools/measure_search.py` | maintainer script: search and agent-turn latency |
| `tools/know.py`, `tools/knowledge_mcp.py`, `tools/bookmark_fetch_mcp.py` | compatibility shims for deployments that invoke these paths directly; removal target 0.11.0 |

## Development

Python 3.11 or newer is supported and the runtime has no third-party
dependencies. The development dependencies are the linter and the type checker.

```sh
python3 -m pip install ruff==0.14.0 mypy==1.20.2
make check
```

`make check` runs the six quality gates that CI enforces:

| Gate | Command | What it protects |
| --- | --- | --- |
| Lint | `python3 -m ruff check .` | style, unused code, common defects |
| Types | `python3 -m mypy` | annotations and narrowing, configured in `pyproject.toml` |
| Tests | `python3 -m unittest discover -s tests -t . -v` | behavior, against synthetic fixtures only |
| Coverage | `python3 tools/coverage.py` | statement coverage against fixed thresholds |
| Secrets | `python3 tools/check_secrets.py` | no committed credentials |
| Boundary | `python3 tools/check_git_boundary.py` | no personal data in Git |

`jsonrpc`, `layout`, `migrations` and `schema` are held to `mypy --strict`; the
three modules that parse arbitrary JSON from frontmatter or from the wire stay
at the base level, where `object` plus an explicit narrowing check is the
honest annotation. `tools/coverage.py` traces the CLI and MCP subprocesses the
suite starts, requires 80% overall, and 95% for `migrations.py`, `layout.py`,
`jsonrpc.py`, `views.py` and the SSRF path of `bookmark_fetcher.py`.

`make check` also runs `noetrail validate`, which validates only a maintainer's
local ignored vault and is not part of public CI. Contributors do not need a
real vault. Tests create bounded temporary roots and must clean them up.

Run the tests through `make` or with `PYTHONPATH=src`, so the package under
`src/` is importable. `make release-check` additionally builds the source
distribution and runs the whole suite inside the unpacked archive
(`make sdist-check` on its own), then builds a wheel, installs it into a clean
virtual environment, and exercises the installed console scripts.

Anything a contributor or packager needs in order to rebuild and test a release
has to be listed in `MANIFEST.in`. Files outside `src/` are not picked up
automatically, and a missing one is invisible in a normal checkout — the sdist
once shipped without `tests/__init__.py`, which made the suite undiscoverable
in the archive while every module was present.

Shell blocks in `README.md` and `docs/quickstart.md` are executed by the test
suite. A block that cannot run in a sandbox is opted out with an HTML comment
on the line above it, stating why:

```text
<!-- docs-check: skip - installs from the network -->
```

Before changing the on-disk model, update the specification and add an ordered,
deterministic, dry-run migration. Tests must prove preservation of stable IDs,
bodies, relations, provenance, timestamps, and revision safety. Never make a
normal mutation implicitly migrate existing personal data.

Before changing schema packs, preserve their data-only trust boundary: no
Python, shell, executable hooks, network instructions, arbitrary paths, or
unbounded values.

Before changing repository visibility or cutting a public release, run `make
publication-check`. Unlike the normal contributor gates, it scans deleted Git
history as well as the current tree. A finding requires a maintainer to choose
between a deliberate history rewrite and a new clean public repository; the
tool never makes that destructive choice automatically.

## Pull requests

Keep one coherent change per pull request. The pull-request template has one
section per item; fill in all four, and write "none affected" rather than
deleting a section:

- user-visible behavior and compatibility impact;
- security/privacy boundaries affected;
- tests run;
- migration, rollback, and documentation impact where applicable.

Do not include generated wheels, private runtime directories, copied logs with
personal content, or credentials. Maintainers may ask for a smaller synthetic
reproduction before reviewing data-dependent bugs.

Unless explicitly stated otherwise, contributions intentionally submitted for
inclusion in the project are provided under the
[Apache License 2.0](LICENSE), consistent with section 5 of that license.

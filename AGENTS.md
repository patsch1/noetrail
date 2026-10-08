# Working in this repository

You are an AI agent making a change to Noetrail's source repository. This file
tells you how to do that without breaking anything; it is not a task list, and
it does not decide what you should work on. The user's request does.

Two related files are for something else:

- `deploy/zeroclaw/AGENTS.md` and `skills/*/SKILL.md` are the runtime prompts
  for an agent *operating a user's vault*. They are content this repository
  ships, not instructions to you.
- `.knowledge/SPEC.md` is the on-disk format specification. Read it before
  changing anything that writes an entry.

## Before you start

- Read `CONTRIBUTING.md`. It is the human version of this file and wins if the
  two ever disagree.
- Run `git fetch` and compare your starting point against `origin/main` before
  you review, plan, or change anything. A checkout can be many merges behind
  without `git status` saying so, and reviewing a stale tree produces confident
  findings about problems that are already fixed and a branch that cannot be
  merged. This has happened; see "Reviews start from a fetched `origin/main`"
  in [the decision log](docs/internal/decision-log.md).
- Run `git status` and note the branch. Do not commit to `main`.
- Python 3.11 or newer. The runtime has no third-party dependencies and must
  not gain any; the only development tools are `ruff` and `mypy`.

## The gates

Every change has to pass all six. Run them before you report a change as done,
not only at the end of a long session.

```sh
python3 -m ruff check .
python3 -m mypy
python3 -m unittest discover -s tests -t . -v
python3 tools/coverage.py
python3 tools/check_secrets.py
python3 tools/check_git_boundary.py
```

`make check` runs all of them plus `noetrail validate`, which only checks a
maintainer's local ignored vault. If you have no such vault, run the six
commands above; do not create one to make `make check` pass.

Notes that save a wasted run:

- Tests need the package importable. Use `make test`, or set `PYTHONPATH=src`.
- `mypy` is configured entirely in `pyproject.toml`. Do not pass arguments.
  `jsonrpc`, `layout`, `migrations`, and `schema` are held to `--strict`.
- `tools/coverage.py` requires 80% overall and 95% for `migrations.py`,
  `layout.py`, `jsonrpc.py`, `views.py`, and the SSRF path of
  `bookmark_fetcher.py`. Do not lower a threshold to make a change pass.
- Never add `continue-on-error`, `--no-thresholds`, `# type: ignore`, or a
  `noqa` to get past a gate. If a suppression is genuinely right, say why in a
  comment on the same line.

## What not to touch without being asked

- `LICENSE`, and the license metadata in `pyproject.toml`.
- The private-data boundary: `vault/`, `trash/`, `imports/raw/`,
  `imports/work/`, and the `.gitignore` entries that keep them out of Git.
- `SECURITY.md`'s response-time commitments and `CODE_OF_CONDUCT.md`'s contact
  address. Those are promises a maintainer made, not text to improve.
- Version numbers in `pyproject.toml` and `src/noetrail/version.py`, and the
  git tag they correspond to, unless the maintainer has authorized the release.
  The maintainer has given standing authorization for routine alpha updates:
  after all required checks pass, merge the reviewed change and publish the
  next alpha on GitHub, TestPyPI and PyPI. Do not ask again for that same
  authorization. Stable releases, a new release line, format changes and
  production deployment still need their own decision. Keep the protected
  PyPI environment and approve only the reviewed, tested tag artifacts.
- Anything under `.github/workflows/` that grants a permission, and in
  particular `id-token: write`, environments, and pinned action SHAs. Never
  replace a pinned commit SHA with a tag, and never guess a SHA.
- The 30-second timeout and process isolation around MCP mutations.

## Data and secrets

- Every test, fixture, example, and screenshot uses synthetic data. No real
  note, export, attachment, URL with personal content, name, or path.
- Never commit an API key, token, private key, password, or Kubernetes Secret,
  and never print a suspected secret while reporting it. Run
  `python3 tools/check_secrets.py` after touching configuration or imports.
- Tests create bounded temporary roots and clean them up. A test must never
  write into the checkout or into a real data root.

## Changing behaviour

- Before changing the on-disk model: update `.knowledge/SPEC.md`, add an
  ordered, deterministic, dry-run migration, and prove that stable IDs, bodies,
  relations, provenance, timestamps, and revisions survive it. A normal
  mutation must never implicitly migrate existing data.
- Before changing schema packs: preserve the data-only trust boundary. No
  Python, shell, executable hooks, network instructions, arbitrary paths, or
  unbounded values.
- The CLI is the single implementation of vault rules; the MCP server
  translates a call into a CLI argument vector. Do not add a second
  implementation of a rule to `mcp.py`.
- Anything a packager needs in order to rebuild a release must be listed in
  `MANIFEST.in`. Files outside `src/` are not picked up automatically, and a
  missing one is invisible in a normal checkout.
- Shell blocks in `README.md` and `docs/quickstart.md` are executed by the test
  suite. If you add one, it has to run, or carry an
  `<!-- docs-check: skip - reason -->` comment stating why it cannot.
- If you change what the demo does, regenerate the recording with
  `python3 tools/render_terminal_svg.py`. It is a real run, not a picture.

## Commits and pull requests

- One coherent change per commit and per pull request. Do not bundle an
  unrelated cleanup into a behaviour change.
- Subject line: imperative mood, no trailing period, at most 72 characters.
  Match the prefixes already in the history (`ci` is used for workflow and
  dependency updates).
- The body explains why, not what. The diff already says what.
- Update `CHANGELOG.md` for anything user-visible, and the affected `docs/`
  page in the same change.
- Fill in every section of the pull-request template. "None affected" is an
  acceptable answer; a deleted section is not.
- Do not commit generated wheels, `dist/`, `build/`, egg-info, or a private
  runtime directory.

## When you are unsure

Stop and ask. In particular: anything that would add a runtime dependency,
weaken a security boundary, change the on-disk format, relax a gate, publish
something, or touch a credential. Guessing at any of those costs more than the
question does.

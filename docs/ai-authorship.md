# How this project was built

Noetrail is vibe-coded. The code, the tests, and this documentation were
written by AI coding agents working from a single maintainer's instructions.
This is not a hand-written core with some generated helpers around it — the
whole repository was produced that way, from the first commit onward.

It is stated up front because a reader deciding whether to run this against
their private notes deserves to know it before they read anything else, not
after they find it in the commit trailers.

## What is actually verifiable

- The public history starts with a reviewed source snapshot on 2026-10-07.
  Earlier development history remains in a private archive because it also
  contained operational notes and private context. Its `Co-Authored-By`
  trailers are therefore not publicly verifiable; this page discloses the
  authorship of that earlier code.
- [`AGENTS.md`](../AGENTS.md) is the instruction file the agents work from: the
  gates they have to pass, what they must not touch, and when they have to stop
  and ask. It is the closest thing this project has to a description of its own
  authorship process.
- [The decision log](internal/decision-log.md) records what was decided and
  why, including the mistakes that led to a rule being added.

## What stands in for a human typing every line

Generated code that nobody checks is a liability. What this repository relies
on instead is that a change cannot land until it passes six gates, all of them
enforced in CI and none of them waivable by the agent that wrote the change:

| Gate | What it protects |
| --- | --- |
| `ruff` | style, unused code, common defects |
| `mypy` | annotations and narrowing, `--strict` for the four modules that parse untrusted input |
| `unittest` | behaviour, against synthetic fixtures only |
| `tools/coverage.py` | 80% overall, 95% for migrations, layout, JSON-RPC, saved views, and the SSRF path |
| `tools/check_secrets.py` | no committed credentials |
| `tools/check_git_boundary.py` | no personal data in Git |

Beyond the gates, the design choices that limit blast radius are deliberate
and predate any individual change: the runtime has no third-party
dependencies, Markdown stays the source of truth so nothing is trapped in a
format only this program can read, schema packs are data and cannot execute,
every mutation is revision-checked and reversible, and whole-entry deletion
goes through trash rather than `unlink`. A bug in generated code is still a
bug; these boundaries decide how much it can cost.

The tests are the part worth reading with the most suspicion, because tests
written by the same process that wrote the code can agree with it and still
both be wrong. Where that mattered most — migrations, the private-data
boundary, the SSRF path, the saved-view parser — the tests assert against
fixed expected output and a higher coverage floor rather than against the
implementation's own behaviour.

## What it does not mean

It does not dilute responsibility. One human maintainer directs the work,
reviews it, and is accountable for what ships. The response times in
[`SECURITY.md`](../SECURITY.md) are commitments a person made, and the
[code of conduct](../CODE_OF_CONDUCT.md) is enforced by that person.

It also does not mean the code is unreviewed, or that a report will be
dismissed because "the agent wrote it". A bug is a bug. If you find one,
[`SUPPORT.md`](../SUPPORT.md) says how to file it and
[`SECURITY.md`](../SECURITY.md) says where a vulnerability goes instead.

## If that is a dealbreaker

That is a legitimate position, and this page exists so you can take it early.
The project is Apache-2.0 licensed and the vault format is plain Markdown with
YAML frontmatter, specified in [`.knowledge/SPEC.md`](../.knowledge/SPEC.md).
Nothing you capture with it is locked to this implementation.

## Public static analysis

CodeQL runs on public changes alongside the six gates. Its security findings
are reviewed against the actual data flow: the secret-scanner regressions
write deliberately fake, generated tokens only inside temporary directories,
and the scanner reports a path, line and fixed category without copying a
matched value. These test cases remain enabled. A reviewed false positive
gets an explicit explanation in the alert record rather than disabling the
query. Standalone test bootstrap imports are also retained for their required
source-path setup. Code quality findings improve assertion diagnostics and
make intentional exits and silent HTTP logging explicit.

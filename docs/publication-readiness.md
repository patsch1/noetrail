# Public alpha readiness

Repository publication and a package release are separate decisions. Publishing
the source makes the Git history, issues, pull requests and Actions output
visible; creating the release tag also starts the package upload workflow.
The first public alpha was `0.10.0a1`. The current `0.10.0a6` outbox-fix
release preserves core schema 12 and the dependency-free runtime; see its
[release notes](releases/0.10.0a6.md).

The public repository starts from a reviewed source snapshot dated 2026-10-07.
Earlier development commits and repository conversations remain private.
[AI authorship](ai-authorship.md) explains the resulting provenance limit.
The first GitHub prerelease, PyPI and TestPyPI uploads are available. The
three-to-five-person user pilot remains the next separate decision.

## Technical acceptance

Before approving the source publication, verify the final merged commit:

- All six contributor gates pass: lint, types, tests, coverage, secrets and
  the Git/private-data boundary. Without a maintainer vault, use the six
  commands in [Contributing](../CONTRIBUTING.md); do not create a real vault.
- `make publication-check` passes after fetching every repository branch and
  GitHub pull-request head as described in [Release process](releasing.md).
  It includes reachable history and release consistency, not only the current
  working tree. Resolve any finding before making the repository public.
- `make release-check` accepts the source distribution and installed wheel.
  Its stdio MCP client verifies grounded aliases, tentative word-form
  candidates, full source bodies, explicit body truncation, absent results and
  wrong-type diagnostics in both read modes, with and without an index.
- `tools/check_reproducible.py` accepts a second build with the same
  `SOURCE_DATE_EPOCH`. The wheel must match byte for byte; the source archive
  must match in content and file modes.
- The demo, documented quickstart, schema upgrade and backup restore pass
  against synthetic data. The separated-root restore test also covers a
  custom pack, saved view, relation and attachment with the original roots
  unavailable.
- Release notes describe the actual hybrid default, word-form candidate
  labels, agent retries and preview-first merging, including limitations.

## Retrieval acceptance and limits

Run all three locally authored diagnostic sets described in
[Retrieval evaluation](retrieval-evaluation.md). On 2026-10-07 the existing
metrics were reproduced:

| Set | Hybrid result | Absent-answer result |
| --- | --- | --- |
| 20-query lexical regression | Recall@3 1.000 | No absent-answer cases |
| 100 everyday questions | Recall@5 0.427; precision 0.727; MRR@5 0.420 | 24/25 searches empty |
| 20 word-form transfer cases | 8/12 answerable queries recovered | 8/8 searches empty; no irrelevant results |

These numbers measure search candidates, not model answers. Retain the misses
and unanswerable cases; do not claim general reliability or a zero
false-positive rate from these small sets.

A fresh client evaluation must cover exact names, paraphrases, language
changes, misspellings, word forms, a topic stored under a different type,
dated relationships and an absent fact. Grade the full source evidence and
the final answer, including actual tool calls and query retries. A model or
tool failure is a failed run, never a correct empty answer. Run both direct
and delegated requests when the host supports delegation.

The [fresh client check](retrieval-evaluation.md#fresh-client-check) records the
2026-10-07 direct/delegated synthetic evaluation, observed response failures and
the final targeted counterchecks after correction. It records source evidence
and bounded abstention rather than treating a successful model exit as a pass.

The [three-to-five-person alpha pilot](alpha-pilot.md) remains a separate
usability exercise. An automated client evaluation cannot establish that
uncoached people can install, import, capture, restore and understand the
product. Recruit volunteers explicitly and record only anonymous, consented
observations. No pilot completion is claimed by this preparation.

## Source publication decision

1. Review the final commit and its successful CI and publication checks.
2. Review repository metadata, issues, pull requests and retained Actions
   artifacts as well as source history for material intended to remain private.
   Include earlier description/comment revisions and commits retained only by
   GitHub's pull-request refs; editing the latest text does not erase them.
3. Explicitly approve the repository visibility change. Do not create a release
   tag as part of this step.
4. Update the visibility-dependent documentation listed in
   [Release process](releasing.md#documentation-that-changes-when-a-switch-is-thrown).
5. Confirm CodeQL actually runs after the change and resolve its findings.
   A skipped private-repository job is not a completed code scan.

## Package publication decision

Before tagging, complete the separate [publisher setup](releasing.md#one-time-setup-before-the-first-upload):

- Confirm the GitHub `pypi` environment restricts deployment to `v*` tags,
  has the required maintainer reviewer and disables administrator bypass.
- Configure and personally verify the TestPyPI and PyPI pending Trusted
  Publishers for `release.yml`, using the exact owner, repository and
  environment identities. No API token is needed.
- Rehearse `Release artifacts` with `workflow_dispatch`. It must accept and
  attest the distributions; its upload jobs must stay skipped without a tag.
- Review the artifacts, SBOM, checksums and compatibility/rollback notes,
  then confirm maintainer authorization for the chosen `v<version>` tag and
  public package upload. The standing authorization described in
  [Release process](releasing.md) covers routine alpha updates after green checks.
- Use the CI-built artifacts for the GitHub prerelease and package indexes;
  do not replace them with local rebuilds. Prepare matching versioned
  documentation in the approved release commit, then verify the package-index
  pages and installation commands after the uploads complete.

The cheaper update of large search indexes and removal of compatibility shims
in `0.11.0` remain future work in [Roadmap](../ROADMAP.md). They are not gates
for this alpha and are not bundled into publication preparation.

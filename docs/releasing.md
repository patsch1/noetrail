# Release process

## Distribution decision

The first public alpha is `0.10.0a1`, available from
[GitHub](https://github.com/patsch1/noetrail/releases/tag/v0.10.0a1),
[PyPI](https://pypi.org/project/noetrail/0.10.0a1/) and
[TestPyPI](https://test.pypi.org/project/noetrail/0.10.0a1/). The number
preserves Noetrail's internal
`0.8.x` and `0.9.x` lineage instead of resetting the project to `0.1.0`.

The current alpha is `0.10.0a3`, with labelled title/alias typo candidates and
bounded retrieval variants. The core schema remains 12. See its
[release notes](releases/0.10.0a3.md).

The intended channels are:

1. a GitHub Release containing the CI-built wheel, source distribution, and
   `SHA256SUMS.txt`;
2. the identical Python artifacts on PyPI under the distribution name
   `noetrail`, uploaded with PyPI Trusted Publishing after the repository and
   first public upload are explicitly approved;
3. no core OCI image for the first alpha. A separately hardened bookmark
   fetcher image may be published later as an integration artifact.

PyPI Trusted Publishing uses short-lived OIDC credentials instead of a stored
long-lived API token. `.github/workflows/release.yml` implements it: no API
token appears anywhere in this repository, and no such token exists for this
project. See the
[official PyPI setup guide](https://docs.pypi.org/trusted-publishers/) and
[publisher configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

The source repository, GitHub prerelease, PyPI and TestPyPI uploads have been
approved and published. The maintainer has authorized routine alpha updates
to include merging and publication after all required checks pass. Advance to
the next alpha in the same release line; stable releases, a new release line,
format changes and production deployment need a separate decision. The `pypi`
environment remains protected: its reviewer approves only the tested tag
artifacts, using this standing authorization for routine alpha updates.

### One-time setup before the first upload

These steps are configuration outside the repository. Until they are done, the
publish jobs fail at the point where they ask for a token, which is the correct
failure: nothing is uploaded.

1. Create the GitHub environments `testpypi` and `pypi` in the repository
   settings. Give `pypi` a required reviewer and restrict its deployment
   branches to tags matching `v*`. `testpypi` needs no reviewer.
2. On [TestPyPI](https://test.pypi.org/manage/account/publishing/), add a
   pending publisher: project `noetrail`, owner `patsch1`, repository
   `noetrail`, workflow `release.yml`, environment `testpypi`.
3. On [PyPI](https://pypi.org/manage/account/publishing/), add the same pending
   publisher with environment `pypi`.
4. Nothing else. Do not create an API token; the workflow has no input for one.

The public repository uses a `pypi` environment restricted to tags matching
`v*`, with the maintainer as its required reviewer and administrator bypass
disabled. Verify these rules in Settings before every upload. The pending
publishers authorize the exact owner, repository, workflow and environment;
creating them does not upload a distribution or reserve the package name.

### What the release workflow produces

For a matching tag, `release.yml` runs four jobs in order:

| Job | What it does | Extra permission |
| --- | --- | --- |
| `build` | builds sdist and wheel, runs the suite from the unpacked sdist, runs the installed-wheel acceptance, rebuilds and compares, writes the SBOM and `SHA256SUMS.txt` | none |
| `attest` | signs a build-provenance attestation for the wheel and the sdist | `id-token: write`, `attestations: write` |
| `publish-testpypi` | uploads the two distributions to TestPyPI | `id-token: write` |
| `publish-pypi` | uploads the same files to PyPI after the environment's reviewer approves | `id-token: write` |

The `build` job deliberately has no `id-token: write`. It is the job that runs
the project's own code and the build backend; it must not be able to mint a
credential an index would accept. The publishing jobs receive finished files
and run nothing from the repository.

`workflow_dispatch` builds and attests but does not publish: both upload jobs
are guarded by `startsWith(github.ref, 'refs/tags/v')`.

### Reproducible builds

The workflow sets `SOURCE_DATE_EPOCH` to the tagged commit's own commit date,
then builds a second time from a copy of the tree and compares
(`tools/check_reproducible.py`). The two artifacts behave differently, and this
was measured rather than assumed:

- the **wheel** is byte-for-byte identical across builds, because setuptools
  writes it through the vendored `wheel` writer, which honours
  `SOURCE_DATE_EPOCH`;
- the **source distribution** is not. setuptools' `sdist` command ignores
  `SOURCE_DATE_EPOCH` and copies each file's filesystem modification time into
  the tar header, and `PKG-INFO`, `setup.cfg`, and the `*.egg-info` directory
  are generated during the build, so they carry the build time. Two consecutive
  builds from one unchanged checkout already differ in the archive digest while
  every file inside is identical.

The check therefore requires bitwise equality for the wheel and content
equality — same members, same modes, same bytes — for the sdist, and reports
the number of members that differ only in a recorded timestamp. A difference in
anything else fails the job. Making the sdist bitwise reproducible would need
either a change in setuptools or a post-processing step that rewrites the
archive, which would mean shipping an archive the build backend did not
produce.

Without the optional build frontend, the local reproducibility tool calls the
setuptools backend directly. A generated `build/` directory is not evidence
that the frontend is installed; the tool checks for its runnable module before
choosing that path.

### Attestation and bill of materials

Each release carries:

- a signed build-provenance attestation for the wheel and the sdist, verifiable
  with `gh attestation verify <file> --repo patsch1/noetrail`;
- `noetrail-sbom.cdx.json`, a CycloneDX 1.6 document generated by
  `tools/generate_sbom.py` from `pyproject.toml`. Its `components` array is
  empty because `project.dependencies` is empty — the list is read from the
  metadata, so a dependency added later appears there without anyone
  remembering to update it;
- `SHA256SUMS.txt` over everything above.

The SBOM and the checksum file are attached to the GitHub Release. They are not
uploaded to PyPI, which accepts only distributions.

## Documentation that changes when a switch is thrown

Source publication and package publication happened separately. The following
statements now record both completed steps. Review them together when the
release channel or current alpha changes:

**When the repository becomes public**

| Page | Updated source-publication statement |
| --- | --- |
| `ROADMAP.md` | "The source is public" |
| `ROADMAP.md` | "CodeQL scans public changes" |
| `README.md` | "source repository is public" |

**Published `0.10.0a3` on production PyPI**

| Page | Updated package-publication statement |
| --- | --- |
| `README.md` | "available on PyPI, GitHub and TestPyPI" |
| `docs/quickstart.md` | "`0.10.0a3` is on PyPI" |
| `docs/installation.md` | "The alpha is on PyPI" |
| `docs/integrations/mcp-clients.md` | "The alpha is on PyPI" |

The `<!-- docs-check: skip -->` markers above those `uvx` blocks are
deliberately *not* on this list. They state that `uvx` installs
over the network, which stays true after the upload, so the blocks stay opted
out for a reason that does not expire.

`test_publication_switches_are_documented` fails if one of the quoted phrases
is no longer in its page, so the list cannot quietly go stale — but it cannot
tell whether a switch has been thrown. That part is a person's job.

## Pre-release checklist

Before the history audit, fetch GitHub's pull-request heads as well as branches:

```sh
git fetch origin '+refs/pull/*/head:refs/remotes/origin/pull/*/head'
```

A normal branch fetch misses those read-only refs. Older PR commits can retain
private content after a branch history was cleaned. The audit also examines
commit messages and the operational journal under its earlier filenames.
Review PR/issue bodies and their edit histories separately: a clean Git tree
does not make the entire GitHub repository safe to publish. GitHub documents
[removing cached views and PR references](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository#fully-removing-the-data-from-github)
and [redacting comment revisions](https://docs.github.com/en/communities/moderating-comments-and-conversations/tracking-changes-in-a-comment).

1. Start from a clean `main` checkout.
2. Run `python tools/check_release_consistency.py`; on a tag, CI additionally
   verifies that `v<version>` matches the same source chain.
3. Run `make check`, `make publication-check`, and `make release-check`. The latter includes
   `make sdist-check`, which runs the suite inside the unpacked source
   distribution.
4. Confirm the synthetic demo and five-minute quickstart work from a fresh
   environment. The suite executes the documented shell blocks, so a failure
   here means a block was opted out of that check.
5. Confirm the release contains no real vault, trash, raw import, attachment,
   backup, credential, secret, or private operational journal. A history audit
   failure must be resolved with an explicitly approved rewrite or a new clean
   public repository before visibility changes.
6. Review compatibility and rollback notes.
7. Create annotated tag `v0.10.0a3` only after publication approval.

## Artifact workflow

`.github/workflows/release.yml` runs manually or for a final, alpha, beta, or
release-candidate tag. The `build` job builds the source distribution and the
wheel, runs the full test suite inside the unpacked source distribution,
installs the wheel into a fresh virtual environment, migrates a synthetic
schema-8 fixture to the current schema 12, validates the upgrade, restores the saved
schema-8 boundary, validates the restore, and exercises the installed stdio
MCP server from a neutral directory. MCP acceptance checks aliases, word-form
candidate labels, full source bodies, explicit truncation, absent answers and
the wrong-type diagnostic in both in-process and subprocess read modes, with
and without an index. It then rebuilds and compares, generates the
bill of materials, and uploads everything plus the SHA-256 manifest as a GitHub
Actions artifact.

Track the separate source-publication and package-publication decisions in
[Publication readiness](publication-readiness.md). A successful deterministic
MCP client run does not establish model answer quality or complete the
[alpha pilot](alpha-pilot.md).

Both artifacts are built in the same run so that the wheel and the source
distribution PyPI receives are the ones that were reviewed. The publish jobs
download that artifact; they never build.

After reviewing the artifact, create the prerelease from the downloaded files
without rebuilding them:

```sh
gh release create v0.10.0a3 \
  --prerelease \
  --title "Noetrail 0.10.0a3" \
  --notes-file docs/releases/0.10.0a3.md \
  dist/noetrail-0.10.0a3-py3-none-any.whl \
  dist/noetrail-0.10.0a3.tar.gz \
  dist/noetrail-sbom.cdx.json \
  dist/SHA256SUMS.txt
```

Do not substitute a locally rebuilt wheel or source distribution after review.
PyPI must receive the same reviewed artifacts.

A consumer can check a downloaded file against the workflow that produced it.
Download the distributions, SBOM and checksum manifest into the same directory;
the manifest uses filenames relative to that directory, without build paths:

```sh
gh attestation verify noetrail-0.10.0a3-py3-none-any.whl --repo patsch1/noetrail
sha256sum --check SHA256SUMS.txt
```

## Rollback

Program rollback selects the previous immutable release. If a schema migration
has written private data, restore the matching pre-migration data and configuration
backups and previous program version together. Never lower `schema_version` manually.

The release acceptance test performs this sequence with synthetic data. A
production deployment must still verify its own storage backup and restore
mechanism.

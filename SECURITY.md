# Security policy

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue.

Preferred channel: the repository's
[private vulnerability report form](https://github.com/patsch1/noetrail/security/advisories/new).
GitHub Private Vulnerability Reporting is enabled for this repository, so the
report, the discussion, the draft advisory, and any requested CVE stay private
until the advisory is published.

If GitHub is unavailable to you, write to <patrick.schiffler@yahoo.de> with
`noetrail security` in the subject. That mailbox is not encrypted; send only
what is needed to reproduce the problem, and prefer the GitHub form for
anything sensitive.

Include a minimal synthetic reproduction, the affected version or commit, the
Python version, and the operating system. Never submit a real vault,
attachment, export, API token, Kubernetes Secret, private key, or password.

## What to expect

Noetrail is maintained by one person. The commitments below are what a single
maintainer can actually keep; they are deliberately not a commercial SLA.

The code, tests, and documentation were written by AI coding agents under
maintainer direction. The maintainer remains responsible for releases and
security reports. Findings are assessed against the implementation and their
impact. See [Development process and AI authorship](docs/ai-authorship.md).

| Step | Target |
| --- | --- |
| Acknowledgement that the report arrived | within 72 hours |
| First assessment: accepted, needs more information, or not a vulnerability | within 10 days |
| Fix or documented mitigation for an accepted report | within 90 days of the acknowledgement |
| Public advisory | at the same time as the fix, or at 90 days at the latest |

The 90-day clock is the disclosure deadline for both sides. If a fix needs
longer, the maintainer will say so before the deadline and agree an extension
with the reporter; if there is no agreement, the reporter is free to disclose
at 90 days. A vulnerability already being exploited is disclosed as soon as a
mitigation exists, without waiting.

Reporters are credited in the advisory by the name they choose, or not at all
if they prefer. The project has no bug bounty.

## Supported versions

| Version | Supported |
| --- | --- |
| `0.10.x` (current alpha line) | yes |
| `main` | yes |
| `0.9.x` and older internal lines | no; upgrade to `0.10.x` |

Before `1.0`, security fixes target the latest released line and the current
development branch only. There is no backport line: a fix is released as a new
version of the current line, and older pre-release lines require upgrading, or
restoring a compatible data snapshot as described in
[backup and restore](docs/backup-restore.md). This section is updated with
every release; the versioning rules are in [versioning](docs/versioning.md).

## Security boundaries

The project protects data through capability separation and filesystem
boundaries; sensitivity labels are not encryption. In particular:

- private data roots, trash, raw imports, and attachments stay out of Git;
- the Noetrail MCP exposes typed operations, not arbitrary shell or paths;
- the bookmark fetcher has network access but no vault or Noetrail MCP access;
- external content cannot authorize tools, deletion, secrets, or outbound work;
- attachment ingestion is limited to a fixed process-start inbox;
- destructive purge remains a separate local operation with preview semantics.

See [the security architecture](docs/integrations/zeroclaw-security.md) and
[privacy boundaries](docs/privacy.md) for operational details.

## Supply chain

Release artifacts are built by
[`.github/workflows/release.yml`](.github/workflows/release.yml) and can be
checked without trusting the maintainer's machine:

- every third-party GitHub Action is pinned to a commit SHA, and Dependabot
  updates the SHA and its version comment together;
- the release job builds the distribution twice and compares the results; the
  wheel is bit-for-bit reproducible from `SOURCE_DATE_EPOCH`, the source
  distribution is compared by content (see `tools/check_reproducible.py` for
  why the two differ);
- wheel and source distribution carry a signed build-provenance attestation,
  verifiable with `gh attestation verify <file> --repo patsch1/noetrail`;
- a CycloneDX bill of materials is published with each release, generated from
  the packaging metadata by `tools/generate_sbom.py`; it lists no runtime
  components because the project declares no runtime dependencies;
- uploads to PyPI use Trusted Publishing from a protected environment. No
  long-lived API token exists for this project.

The release process, including what still requires an explicit human gate, is
in [Release process](docs/releasing.md).

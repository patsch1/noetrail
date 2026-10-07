# Support

Use GitHub issues for reproducible bugs, documentation problems, and bounded
feature proposals. This is a one-person community project and does not provide
an SLA, paid support, hosted storage, or emergency recovery service.

Open-ended questions, agent setups, and ideas that are not yet a proposal
belong in [Discussions](https://github.com/patsch1/noetrail/discussions).

## Filing an issue

The three issue forms — bug, feature, documentation — require the program
version, the Python version, the operating system, and a `noetrail doctor`
report. Blank issues are disabled, because a report without those costs one
round trip before anything can be looked at.

Before filing:

1. Run `noetrail doctor` and `noetrail validate`.
2. Reproduce the problem with a synthetic temporary vault when possible.
3. Remove titles, bodies, URLs, paths containing personal names, attachments,
   exports, tokens, and infrastructure details from anything you paste.
4. Have the sanitized error or exit code, and whether you used the CLI or MCP.

Security reports belong in the private channel described in
[SECURITY.md](SECURITY.md), not in public support issues. That policy states
the acknowledgement and disclosure times you can expect.

Questions about a specific ZeroClaw deployment should distinguish repository
behavior from the separate infrastructure configuration.

<!--
The four sections below are the four things CONTRIBUTING.md asks a pull
request to describe. Keep one coherent change per pull request.
Delete a section only if you replace it with a sentence saying why it does not
apply.
-->

## Summary

<!-- What changes, and why. One paragraph. -->

## User-visible behavior and compatibility

<!--
Which command, MCP tool, output field, or file layout changes, and what an
existing installation notices. Write "none" if nothing user-visible changes.
-->

## Security and privacy boundaries

<!--
Which boundary this touches: the Git/private-data separation, the narrow MCP
surface, the isolated bookmark fetcher, attachment ingestion, purge, secrets.
Write "none affected" if it touches none of them.
-->

## Tests run

<!--
Paste the commands you ran and their result. `make check` covers all six
gates; name the individual gates if you ran them separately.
-->

## Migration, rollback, and documentation

<!--
On-disk format changes need an ordered, deterministic, dry-run migration and a
rollback path. Name the documents you updated, or say why none needed it.
-->

---

- [ ] `make check` passes locally (lint, types, tests, coverage, secrets, boundary).
- [ ] Tests and fixtures use synthetic data only — no real vault, export, attachment, URL, or credential.
- [ ] Documentation, `CHANGELOG.md`, and any affected `docs/` page are updated.
- [ ] No generated wheels, private runtime directories, or copied logs are included.
- [ ] I am submitting this under the [Apache License 2.0](../LICENSE), per section 5 of that license.

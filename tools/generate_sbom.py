#!/usr/bin/env python3
"""Generate a CycloneDX software bill of materials for this distribution.

The project ships no runtime dependencies and its build environment has no
package index, so an external SBOM generator is neither available nor needed.
The interesting statement -- "this release pulls in nothing else at runtime" --
is only worth anything if it is derived from the packaging metadata instead of
being typed into a document by hand, which is what this script does.

Sources
-------
``pyproject.toml`` is the authority for name, version, description, license,
supported interpreters, project URLs, and ``project.dependencies``. The
dependency list is read, not assumed: if a runtime dependency is ever added,
it appears as a component here without anyone remembering to update the SBOM.

Passing ``--distribution`` records the built artifacts the SBOM describes,
each with its SHA-256 digest, so the document can be checked against the files
in a release rather than only against a version string.

Determinism
-----------
``SOURCE_DATE_EPOCH`` fixes ``metadata.timestamp`` and the serial number is
derived from name and version, so two runs over the same tree produce the same
bytes. That matters because the SBOM is published beside artifacts whose
reproducibility is itself checked.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib
import uuid

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CYCLONEDX_SPEC_VERSION = "1.6"
# A CycloneDX serial number identifies one BOM. Deriving it from the project
# and its version keeps the document stable across rebuilds instead of making
# every regeneration look like a different bill of materials.
SERIAL_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# `name [extras] (specifier) ; marker` -- only the leading name is needed to
# name a component, and PEP 508 names are a bounded grammar.
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")

REFERENCE_TYPES = {
    "Homepage": "website",
    "Repository": "vcs",
    "Issues": "issue-tracker",
    "Changelog": "release-notes",
}


def build_timestamp() -> str:
    """UTC build time, pinned by SOURCE_DATE_EPOCH when the caller sets it."""

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = (
        datetime.fromtimestamp(int(epoch), tz=UTC)
        if epoch
        else datetime.now(tz=UTC)
    )
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def dependency_components(dependencies: list[str]) -> list[dict[str, object]]:
    """One component per declared runtime dependency; today an empty list.

    An empty ``components`` array is the verifiable claim this SBOM exists to
    make. It stays empty only for as long as ``project.dependencies`` is.
    """

    components: list[dict[str, object]] = []
    for requirement in dependencies:
        match = REQUIREMENT_NAME.match(requirement)
        if match is None:
            raise SystemExit(f"unparsable requirement: {requirement!r}")
        name = match.group(1)
        components.append(
            {
                "type": "library",
                "bom-ref": f"pkg:pypi/{name.lower()}",
                "name": name,
                "purl": f"pkg:pypi/{name.lower()}",
                "scope": "required",
                "properties": [
                    {"name": "noetrail:requirement", "value": requirement.strip()}
                ],
            }
        )
    return components


def distribution_references(paths: list[Path]) -> list[dict[str, object]]:
    references: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda item: item.name):
        if not path.is_file():
            raise SystemExit(f"distribution file does not exist: {path}")
        references.append(
            {
                "type": "distribution",
                "url": path.name,
                "comment": "release artifact this bill of materials describes",
                "hashes": [{"alg": "SHA-256", "content": digest(path)}],
            }
        )
    return references


def project_references(urls: dict[str, str]) -> list[dict[str, object]]:
    references: list[dict[str, object]] = []
    for label, reference_type in REFERENCE_TYPES.items():
        url = urls.get(label)
        if url:
            references.append({"type": reference_type, "url": url})
    return references


def build_document(
    metadata: dict[str, object], distributions: list[Path]
) -> dict[str, object]:
    project = metadata["project"]
    assert isinstance(project, dict)
    name = str(project["name"])
    version = str(project["version"])
    purl = f"pkg:pypi/{name}@{version}"

    urls = project.get("urls", {})
    assert isinstance(urls, dict)
    references = project_references(urls) + distribution_references(distributions)

    authors = [
        {"name": str(author["name"])}
        for author in project.get("authors", [])
        if isinstance(author, dict) and author.get("name")
    ]

    root: dict[str, object] = {
        "type": "application",
        "bom-ref": purl,
        "name": name,
        "version": version,
        "description": str(project.get("description", "")),
        "purl": purl,
        "licenses": [{"license": {"id": str(project["license"])}}],
        "externalReferences": references,
        "properties": [
            {
                "name": "noetrail:requires-python",
                "value": str(project["requires-python"]),
            },
            {
                "name": "noetrail:runtime-dependency-count",
                "value": str(len(project.get("dependencies", []))),
            },
        ],
    }
    if authors:
        root["authors"] = authors

    dependencies = project.get("dependencies", [])
    assert isinstance(dependencies, list)
    components = dependency_components([str(item) for item in dependencies])

    return {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid5(SERIAL_NAMESPACE, purl)}",
        "version": 1,
        "metadata": {
            "timestamp": build_timestamp(),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "noetrail-generate-sbom",
                        "version": version,
                    }
                ]
            },
            "component": root,
        },
        "components": components,
        "dependencies": [
            {
                "ref": purl,
                "dependsOn": [str(component["bom-ref"]) for component in components],
            }
        ],
    }


def render(document: dict[str, object]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=REPOSITORY_ROOT / "pyproject.toml",
        help="packaging metadata to derive the bill of materials from",
    )
    parser.add_argument(
        "--distribution",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="release artifact to record with its SHA-256 digest; repeatable",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write here instead of standard output",
    )
    args = parser.parse_args(argv)

    with args.pyproject.open("rb") as handle:
        metadata = tomllib.load(handle)
    text = render(build_document(metadata, list(args.distribution)))

    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

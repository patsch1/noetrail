#!/usr/bin/env python3
"""Keep package, source, schema, changelog, notes, and release tag aligned."""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re
import tomllib


def literal_assignment(path: Path, name: str) -> str | int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != name:
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str | int):
            return value
        break
    raise ValueError(f"{path}: {name} is not a literal string or integer")


def check(root: Path, *, tag: str | None = None) -> list[str]:
    failures: list[str] = []
    with (root / "pyproject.toml").open("rb") as handle:
        metadata = tomllib.load(handle)
    version = metadata["project"]["version"]
    configured_schema = metadata["tool"]["noetrail"]["schema-version"]
    source_version = literal_assignment(
        root / "src" / "noetrail" / "version.py",
        "FALLBACK_VERSION",
    )
    source_schema = literal_assignment(
        root / "src" / "noetrail" / "migrations.py",
        "CURRENT_SCHEMA_VERSION",
    )

    if source_version != version:
        failures.append(
            f"package version {version!r} != source fallback {source_version!r}"
        )
    if source_schema != configured_schema:
        failures.append(
            f"schema version {source_schema!r} != pyproject {configured_schema!r}"
        )

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_heading = f"## {version} - Unreleased"
    if changelog_heading not in changelog:
        failures.append(f"CHANGELOG.md is missing {changelog_heading!r}")

    notes_path = root / "docs" / "releases" / f"{version}.md"
    if not notes_path.is_file():
        failures.append(f"release notes are missing: docs/releases/{version}.md")
    else:
        notes = notes_path.read_text(encoding="utf-8")
        if f"# Noetrail {version}" not in notes:
            failures.append("release notes heading does not match package version")
        schema_claim = re.compile(
            rf"current core schema is\s+{source_schema}\b",
            re.IGNORECASE,
        )
        if schema_claim.search(notes) is None:
            failures.append(
                f"release notes do not state current core schema {source_schema}"
            )

    if tag is not None:
        expected_tag = f"v{version}"
        if tag != expected_tag:
            failures.append(f"release tag {tag!r} != expected {expected_tag!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(prog="check-release-consistency")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--tag")
    args = parser.parse_args()
    tag = args.tag
    if tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
    root = args.root.expanduser().resolve()
    try:
        failures = check(root, tag=tag)
    except (KeyError, OSError, SyntaxError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"ERROR release consistency could not be checked: {exc}")
        return 1
    for failure in failures:
        print(f"ERROR {failure}")
    if failures:
        print("Release consistency check failed.")
        return 1
    print("Release consistency check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

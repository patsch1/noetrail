#!/usr/bin/env python3
"""Audit every reachable Git blob without echoing sensitive values or paths."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import shutil
import subprocess

from check_secrets import (
    SECRET_PATTERNS,
    SENSITIVE_FILENAMES,
    SENSITIVE_SUFFIXES,
)

PRIVATE_DIRECTORIES = (
    "vault/",
    "trash/",
    "imports/raw/",
    "imports/work/",
)
MAX_SCANNED_BLOB_BYTES = 2 * 1024 * 1024
RETIRED_JOURNAL_HEADINGS = (
    "# Decision and progress log",
    "# Open-source roadmap",
)
PRIVATE_COMMIT_CONTEXT = {
    "private chat session link": re.compile(r"https://claude\.ai/code/session_[\w-]+"),
    "production vault statistics": re.compile(
        r"\bproduction\s+(?:vault\s+)?validation:\s*\d+\s+active\b", re.IGNORECASE
    ),
}
GIT = shutil.which("git")


def run_git(
    root: Path,
    *arguments: str,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if GIT is None:
        raise RuntimeError("git executable is unavailable")
    return subprocess.run(
        [GIT, "-C", str(root), *arguments],
        input=input_data,
        capture_output=True,
        check=False,
    )


def reachable_objects(root: Path) -> dict[str, str]:
    result = run_git(root, "rev-list", "--objects", "--all")
    if result.returncode != 0:
        raise RuntimeError("could not enumerate Git history")
    objects: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        object_name, separator, raw_path = raw_line.partition(b" ")
        if not object_name:
            continue
        path = raw_path.decode("utf-8", errors="replace") if separator else ""
        objects[object_name.decode("ascii")] = path
    return objects


def blob_metadata(root: Path, object_names: list[str]) -> dict[str, int]:
    if not object_names:
        return {}
    result = run_git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_data=("\n".join(object_names) + "\n").encode("ascii"),
    )
    if result.returncode != 0:
        raise RuntimeError("could not inspect Git objects")
    blobs: dict[str, int] = {}
    for raw_line in result.stdout.splitlines():
        fields = raw_line.decode("ascii").split()
        if len(fields) == 3 and fields[1] == "blob":
            blobs[fields[0]] = int(fields[2])
    return blobs


def read_blob(root: Path, object_name: str) -> bytes:
    result = run_git(root, "cat-file", "blob", object_name)
    if result.returncode != 0:
        raise RuntimeError("could not read a Git blob")
    return result.stdout


def private_directory(path: str) -> str | None:
    return next(
        (directory for directory in PRIVATE_DIRECTORIES if path.startswith(directory)),
        None,
    )


def audit(root: Path) -> Counter[str]:
    objects = reachable_objects(root)
    metadata = blob_metadata(root, list(objects))
    findings: Counter[str] = Counter()

    for object_name, size in metadata.items():
        path = objects.get(object_name, "")
        directory = private_directory(path)
        if directory is not None:
            findings[f"private path under {directory}"] += 1
        name = Path(path).name
        suffix = Path(path).suffix.casefold()
        if name in SENSITIVE_FILENAMES:
            findings["sensitive filename"] += 1
        if suffix in SENSITIVE_SUFFIXES:
            findings["sensitive key-file extension"] += 1
        if size > MAX_SCANNED_BLOB_BYTES:
            findings["blob too large for content audit"] += 1
            continue
        payload = read_blob(root, object_name)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings[f"possible {kind}"] += 1
        # The journal previously lived under a different filename. Detect its
        # document shape so a move or copy cannot hide an older private version.
        if (
            path.endswith(".md")
            and text.startswith(RETIRED_JOURNAL_HEADINGS)
            and "Baseline commit:" in text
        ):
            findings["retired operational journal"] += 1

    messages = run_git(root, "log", "--all", "--format=%B%x00")
    if messages.returncode != 0:
        raise RuntimeError("could not inspect commit messages")
    for payload in messages.stdout.split(b"\0"):
        if len(payload) > MAX_SCANNED_BLOB_BYTES:
            findings["commit message too large for content audit"] += 1
            continue
        message = payload.decode("utf-8", errors="replace")
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(message):
                findings[f"possible {kind} in commit message"] += 1
        for kind, pattern in PRIVATE_COMMIT_CONTEXT.items():
            if pattern.search(message):
                findings[kind] += 1
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(prog="check-history")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not (root / ".git").exists():
        print("ERROR Git history audit requires a repository checkout.")
        return 1
    try:
        findings = audit(root)
    except RuntimeError as exc:
        print(f"ERROR {exc}.")
        return 1
    for kind, count in sorted(findings.items()):
        print(f"ERROR Git history contains {count} {kind} finding(s).")
    if findings:
        print(
            "Git history audit failed. Values, object IDs, and affected paths "
            "were intentionally withheld."
        )
        return 1
    print("Git history audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

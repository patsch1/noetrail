#!/usr/bin/env python3
"""Fail without exposing filenames when private data paths enter Git."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import subprocess

PRIVATE_DIRECTORIES = (
    "vault/",
    "trash/",
    "imports/raw/",
    "imports/work/",
)
IGNORE_PROBES = {
    "vault/": "vault/privacy-boundary-probe.md",
    "trash/": "trash/privacy-boundary-probe.md",
    "imports/raw/": "imports/raw/privacy-boundary-probe.md",
    "imports/work/": "imports/work/privacy-boundary-probe.md",
}


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )


def tracked_private_paths(root: Path) -> list[str]:
    result = run_git(
        root,
        "ls-files",
        "-z",
        "--",
        *(item.rstrip("/") for item in PRIVATE_DIRECTORIES),
    )
    if result.returncode != 0:
        raise RuntimeError("Could not inspect the Git index")
    return [
        item.decode("utf-8", errors="replace")
        for item in result.stdout.split(b"\0")
        if item
    ]


def ignored_directories(root: Path) -> set[str]:
    ignored: set[str] = set()
    for directory, probe in IGNORE_PROBES.items():
        result = run_git(
            root,
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            probe,
        )
        if result.returncode == 0:
            ignored.add(directory)
    return ignored


def directory_for(path: str) -> str:
    return next(
        (
            directory
            for directory in PRIVATE_DIRECTORIES
            if path.startswith(directory)
        ),
        "private-data/",
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="check-git-boundary")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()

    if not (root / ".git").exists():
        print("ERROR Git data boundary requires a repository checkout.")
        return 1

    try:
        tracked = tracked_private_paths(root)
    except RuntimeError as exc:
        print(f"ERROR {exc}.")
        return 1
    ignored = ignored_directories(root)

    failed = False
    if tracked:
        counts = Counter(directory_for(path) for path in tracked)
        for directory in PRIVATE_DIRECTORIES:
            if counts[directory]:
                print(
                    f"ERROR {directory} has {counts[directory]} tracked "
                    "private file(s)."
                )
        failed = True

    missing_ignores = [
        directory
        for directory in PRIVATE_DIRECTORIES
        if directory not in ignored
    ]
    for directory in missing_ignores:
        print(f"ERROR {directory} is not protected by .gitignore.")
        failed = True

    if failed:
        print("Git data boundary failed.")
        return 1
    print("Git data boundary passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

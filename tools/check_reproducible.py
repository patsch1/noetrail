#!/usr/bin/env python3
"""Rebuild the distribution and compare it against an existing build.

Why this is not a single ``sha256sum`` comparison
-------------------------------------------------
With ``SOURCE_DATE_EPOCH`` set, the two artifacts behave differently, and the
difference was measured rather than assumed:

* The **wheel** is byte-for-byte reproducible. setuptools builds it through the
  vendored ``wheel`` writer, which reads ``SOURCE_DATE_EPOCH`` and stamps every
  zip entry with it. Two builds from two separate copies of the same tree, with
  different file modification times, produced the same SHA-256.

* The **source distribution** is not. setuptools' ``sdist`` command does not
  read ``SOURCE_DATE_EPOCH`` at all; it copies each file's modification time
  from the filesystem into the tar header. ``PKG-INFO``, ``setup.cfg``, and the
  ``*.egg-info`` directory are generated during the build, so they carry the
  build time -- two consecutive builds from one unchanged checkout already
  differ in the archive digest while every file in them is identical.

So the wheel is held to bitwise equality and the sdist to equality of its
contents: same member names, same file modes, same bytes. A difference in the
recorded modification times is reported and tolerated; anything else fails.
Raising the sdist to bitwise equality needs either a change in setuptools or a
post-processing step that rewrites the archive, which would mean shipping an
archive the build backend did not produce.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COPY_EXCLUDES = (
    ".git",
    "build",
    "dist",
    "*.egg-info",
    "__pycache__",
    ".venv",
    ".mypy_cache",
)


def digest(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def sole(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly one {pattern} in {directory}, found {len(matches)}"
        )
    return matches[0]


def build_command(interpreter: str, output: Path) -> list[str]:
    """Prefer the same frontend CI uses; fall back to the backend directly.

    CI installs ``build``, so the rebuild goes through the identical isolated
    path as the first build. A checkout without a package index has no
    ``build``, and calling the setuptools backend needs no extra tool.
    """

    available = subprocess.run(
        # A local build/ directory can be an importable namespace package even
        # when the frontend is absent. Only its runnable module proves that
        # `python -m build` will work in the isolated rebuild tree.
        [interpreter, "-c", "import build.__main__"],
        capture_output=True,
        check=False,
    )
    if available.returncode == 0:
        return [
            interpreter,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--outdir",
            str(output),
        ]
    return [
        interpreter,
        "-c",
        # `build_sdist` replaces `sys.argv` while it runs the setup command,
        # so the output directory is read once and kept.
        "import sys;import setuptools.build_meta as backend;"
        "target = sys.argv[1];"
        "backend.build_sdist(target);backend.build_wheel(target)",
        str(output),
    ]


def rebuild(source: Path, workspace: Path, interpreter: str) -> Path:
    """Build sdist and wheel from a copy of ``source`` into ``workspace``."""

    tree = workspace / "source"
    shutil.copytree(
        source, tree, ignore=shutil.ignore_patterns(*COPY_EXCLUDES), symlinks=True
    )
    output = workspace / "dist"
    output.mkdir()
    completed = subprocess.run(
        build_command(interpreter, output),
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "rebuild failed:\n" + completed.stdout + completed.stderr
        )
    return output


def sdist_contents(
    archive: Path,
) -> tuple[dict[str, tuple[int, bytes]], dict[str, int]]:
    """Return per-member (mode, bytes) and per-member modification time."""

    contents: dict[str, tuple[int, bytes]] = {}
    times: dict[str, int] = {}
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            times[member.name] = int(member.mtime)
            if not member.isfile():
                contents[member.name] = (member.mode, b"")
                continue
            extracted = handle.extractfile(member)
            payload = b"" if extracted is None else extracted.read()
            contents[member.name] = (member.mode, payload)
    return contents, times


def compare_sdists(reference: Path, rebuilt: Path) -> tuple[list[str], list[str]]:
    """Split the differences into content differences and timestamp-only ones."""

    left, left_times = sdist_contents(reference)
    right, right_times = sdist_contents(rebuilt)

    differences: list[str] = []
    for name in sorted(set(left) - set(right)):
        differences.append(f"only in the reference build: {name}")
    for name in sorted(set(right) - set(left)):
        differences.append(f"only in the rebuild: {name}")
    for name in sorted(set(left) & set(right)):
        mode, payload = left[name]
        other_mode, other_payload = right[name]
        if mode != other_mode:
            differences.append(f"{name}: mode {mode:o} != {other_mode:o}")
        if payload != other_payload:
            differences.append(
                f"{name}: {len(payload)} bytes != {len(other_payload)} bytes"
            )

    timestamps = sorted(
        name
        for name in set(left) & set(right)
        if left_times[name] != right_times[name]
    )
    return differences, timestamps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reference",
        type=Path,
        required=True,
        help="directory holding the already-built wheel and source distribution",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=REPOSITORY_ROOT,
        help="tree to rebuild from (default: the repository root)",
    )
    parser.add_argument(
        "--interpreter",
        default=sys.executable,
        help="interpreter used for the rebuild",
    )
    args = parser.parse_args(argv)

    reference_wheel = sole(args.reference, "*.whl")
    reference_sdist = sole(args.reference, "*.tar.gz")

    with tempfile.TemporaryDirectory() as raw:
        rebuilt = rebuild(args.source, Path(raw), args.interpreter)
        rebuilt_wheel = sole(rebuilt, "*.whl")
        rebuilt_sdist = sole(rebuilt, "*.tar.gz")

        failures: list[str] = []
        wheel_digest = digest(reference_wheel)
        rebuilt_digest = digest(rebuilt_wheel)
        print(f"wheel  reference {wheel_digest}  {reference_wheel.name}")
        print(f"wheel  rebuild   {rebuilt_digest}  {rebuilt_wheel.name}")
        if wheel_digest != rebuilt_digest:
            failures.append(
                "the wheel is not reproducible; SOURCE_DATE_EPOCH is either "
                "unset or something outside the tree leaked into the build"
            )

        sdist_digest = digest(reference_sdist)
        rebuilt_sdist_digest = digest(rebuilt_sdist)
        print(f"sdist  reference {sdist_digest}  {reference_sdist.name}")
        print(f"sdist  rebuild   {rebuilt_sdist_digest}  {rebuilt_sdist.name}")
        if sdist_digest == rebuilt_sdist_digest:
            print("sdist  identical byte for byte")
        else:
            differences, timestamps = compare_sdists(
                reference_sdist, rebuilt_sdist
            )
            if differences:
                failures.append(
                    "the source distribution differs in content:\n  "
                    + "\n  ".join(differences[:20])
                )
            else:
                print(
                    "sdist  same contents, "
                    f"{len(timestamps)} member(s) differ only in the recorded "
                    "modification time (setuptools' sdist ignores "
                    "SOURCE_DATE_EPOCH)"
                )

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

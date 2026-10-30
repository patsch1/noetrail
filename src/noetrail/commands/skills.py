#!/usr/bin/env python3
"""The `skills` commands: find and copy the portable agent skills.

The six workflows under `skills/` are what an MCP host loads to know how to
use the vault tools. A checkout has them next to the source; an installed
wheel has them below `share/noetrail/skills`, a directory nobody should have
to locate by hand. `skills path` prints it, and `skills export` copies the
skills into whatever directory a host reads them from.

Neither command needs or opens a vault. `export` writes only beneath the
target it is given, never follows a symbolic link out of the source or into
the target, and refuses a target inside a data or configuration root it can
see, so exporting cannot put program files into private data.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from noetrail.errors import ConfigurationError, Conflict, InvalidRequest, StorageError
from noetrail.layout import (
    LayoutError,
    default_config_root,
    default_skills_root,
    environment_config_root,
    environment_data_root,
)

# The Agent Skills naming rule: lowercase letters, digits, and inner hyphens.
SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True)
class PlannedFile:
    source: Path
    destination: Path


def resolve_skills_root() -> Path:
    try:
        return default_skills_root()
    except LayoutError as exc:
        raise ConfigurationError(str(exc)) from exc


def skill_names(root: Path) -> list[str]:
    """Every real skill directory below ``root``; links are never followed."""

    return sorted(
        child.name
        for child in root.iterdir()
        if SKILL_NAME.fullmatch(child.name)
        and not child.is_symlink()
        and child.is_dir()
        and (child / "SKILL.md").is_file()
        and not (child / "SKILL.md").is_symlink()
    )


def _skill_files(skill: Path) -> list[Path]:
    """The regular files of one skill, relative to it, refusing any link."""

    files: list[Path] = []
    for directory, subdirectories, filenames in os.walk(skill, followlinks=False):
        current = Path(directory)
        subdirectories[:] = sorted(
            name for name in subdirectories if not name.startswith(".")
        )
        for name in subdirectories:
            if (current / name).is_symlink():
                raise ConfigurationError(
                    f"Skill {skill.name} contains a symbolic link: "
                    f"{(current / name).relative_to(skill)}"
                )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = current / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ConfigurationError(
                    f"Skill {skill.name} contains a non-regular file: "
                    f"{path.relative_to(skill)}"
                )
            files.append(path.relative_to(skill))
    return files


def _protected_roots(args: argparse.Namespace) -> list[Path]:
    candidates: list[str | Path | None] = [
        getattr(args, "root", None),
        getattr(args, "data_root", None),
        getattr(args, "config_root", None),
        environment_data_root(),
        environment_config_root(),
        default_config_root(),
    ]
    return [
        Path(value).expanduser().resolve()
        for value in candidates
        if value is not None
    ]


def _check_destination(path: Path, *, directory: bool) -> None:
    if path.is_symlink():
        raise Conflict(f"Refusing to write through a symbolic link: {path}")
    if not path.exists():
        return
    if directory and not path.is_dir():
        raise Conflict(f"Expected a directory, found a file: {path}")
    if not directory and not path.is_file():
        raise Conflict(f"Expected a file, found something else: {path}")


def _plan_export(
    source_root: Path,
    names: list[str],
    target: Path,
    *,
    force: bool,
) -> list[PlannedFile]:
    occupied: list[str] = []
    plan: list[PlannedFile] = []
    for name in names:
        destination = target / name
        _check_destination(destination, directory=True)
        if destination.is_dir() and any(destination.iterdir()) and not force:
            occupied.append(name)
        for relative in _skill_files(source_root / name):
            output = destination / relative
            for parent in reversed(output.relative_to(target).parents[:-1]):
                _check_destination(target / parent, directory=True)
            _check_destination(output, directory=False)
            plan.append(PlannedFile(source_root / name / relative, output))
    if occupied:
        raise Conflict(
            "Refusing to overwrite existing skill directories in "
            f"{target}: {', '.join(occupied)}; pass --force to replace the "
            "shipped files"
        )
    return plan


def _write_file(planned: PlannedFile) -> None:
    content = planned.source.read_bytes()
    planned.destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=planned.destination.parent,
        prefix=f".{planned.destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Like every other atomic write here: a replaced file keeps its mode,
        # and a new one keeps mkstemp's owner-only mode.
        if planned.destination.exists():
            os.chmod(temporary, planned.destination.stat().st_mode & 0o777)
        os.replace(temporary, planned.destination)
    finally:
        temporary.unlink(missing_ok=True)


def command_skills_path(args: argparse.Namespace) -> int:
    root = resolve_skills_root()
    if args.json:
        print(
            json.dumps(
                {"skills_root": str(root), "skills": skill_names(root)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(root)
    return 0


def command_skills_export(args: argparse.Namespace) -> int:
    source_root = resolve_skills_root()
    names = skill_names(source_root)
    requested = Path(args.directory).expanduser()
    if requested.exists() and not requested.is_dir():
        raise InvalidRequest(f"Export target is not a directory: {requested}")
    target = requested.resolve()
    if target.is_relative_to(source_root):
        raise InvalidRequest(
            f"Export target must not be inside the skills source: {source_root}"
        )
    for protected in _protected_roots(args):
        if target.is_relative_to(protected):
            raise InvalidRequest(
                "Export target must not be inside a Noetrail data or "
                f"configuration root: {target}"
            )

    plan = _plan_export(source_root, names, target, force=args.force)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for planned in plan:
            _write_file(planned)
    except OSError as exc:
        raise StorageError(f"Could not export skills to {target}: {exc}") from exc

    written = [str(planned.destination) for planned in plan]
    if args.json:
        print(
            json.dumps(
                {
                    "skills_root": str(source_root),
                    "target": str(target),
                    "skills": names,
                    "written": written,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Exported {len(names)} skills to {target}:")
        for path in written:
            print(f"  {path}")
    return 0

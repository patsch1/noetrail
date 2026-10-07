#!/usr/bin/env python3
"""Vault filesystem: paths, locking, atomic writes, relocation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import time

from noetrail.constants import LOCK_TIMEOUT_SECONDS, REVISION_PATTERN
from noetrail.deadline import check_deadline
from noetrail.errors import (
    Conflict,
    InvalidRequest,
    RevisionConflict,
    StorageError,
    ValidationError,
    VaultBusy,
)
from noetrail.frontmatter import render_entry
from noetrail.schema import SchemaPackError, SchemaRegistry


def entry_directory(
    root: Path,
    entry_type: str,
    registry: SchemaRegistry | None = None,
) -> Path:
    locations = {
        "thought": "vault/inbox",
        "memory": "vault/memories",
        "note": "vault/notes",
        "person": "vault/entities/people",
        "project": "vault/entities/projects",
        "media": "vault/entities/media",
        "source": "vault/notes",
        "place": "vault/entities/places",
        "product": "vault/entities/products",
        "recipe": "vault/recipes",
        "experience": "vault/experiences",
        "bookmark": "vault/bookmarks",
    }
    if entry_type in locations:
        return root / locations[entry_type]
    if registry is None:
        raise InvalidRequest(f"Unknown entry type: {entry_type}")
    try:
        type_definition = registry.require_type(entry_type)
    except SchemaPackError as exc:
        raise InvalidRequest(str(exc)) from exc
    return (
        root
        / "vault"
        / "custom"
        / type_definition.pack_id
        / type_definition.local_id
    )


def prepare_custom_entry_directory(
    root: Path,
    entry_type: str,
    registry: SchemaRegistry,
) -> Path:
    """Create a derived custom path without following storage symlinks."""

    destination = entry_directory(root, entry_type, registry)
    components = [
        root / "vault",
        root / "vault" / "custom",
        destination.parent,
        destination,
    ]
    for component in components:
        if component.is_symlink():
            raise StorageError(
                "Custom entry storage must not contain symbolic links: "
                f"{component.relative_to(root)}"
            )
        if component.exists() and not component.is_dir():
            raise StorageError(
                "Custom entry storage component is not a directory: "
                f"{component.relative_to(root)}"
            )
        component.mkdir(mode=0o700, exist_ok=True)
    try:
        resolved_vault = (root / "vault").resolve(strict=True)
        resolved_destination = destination.resolve(strict=True)
    except OSError as exc:
        raise StorageError("Custom entry storage is unavailable") from exc
    if not resolved_destination.is_relative_to(resolved_vault):
        raise StorageError("Custom entry storage resolves outside vault/")
    return resolved_destination


def _checked_paths(directory: Path) -> list[Path]:
    paths = []
    if directory.exists():
        for path in directory.rglob("*.md"):
            check_deadline()
            paths.append(path)
    check_deadline()
    return sorted(paths)


def entry_paths(root: Path) -> list[Path]:
    vault = root / "vault"
    return _checked_paths(vault)


def trash_paths(root: Path) -> list[Path]:
    trash = root / "trash"
    return _checked_paths(trash)


def managed_paths(root: Path) -> list[Path]:
    return sorted([*entry_paths(root), *trash_paths(root)])


def revision_for(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def require_expected_revision(path: Path, expected_revision: str | None) -> None:
    if expected_revision is None:
        return
    if not REVISION_PATTERN.fullmatch(expected_revision):
        raise InvalidRequest(
            "Expected revision must use the form sha256:<64 lowercase hex characters>"
        )
    if revision_for(path) != expected_revision:
        raise RevisionConflict(
            "Revision conflict: the entry changed after it was read. "
            "Fetch the current entry and review the change before retrying."
        )


@contextmanager
def vault_lock(root: Path, *, exclusive: bool):
    """Coordinate readers and writers using one advisory lock on the data PVC."""

    lock_directory = root / "vault" / ".locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock_directory / "vault.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    lock_file = os.fdopen(descriptor, "a+")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    try:
        while True:
            check_deadline()
            try:
                fcntl.flock(lock_file.fileno(), operation | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise VaultBusy(
                        "Vault is busy with another operation; retry after it finishes."
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def atomic_write_entry(
    path: Path,
    metadata: dict[str, object],
    body: str,
    *,
    must_not_exist: bool = False,
) -> None:
    if must_not_exist and path.exists():
        raise Conflict(f"Entry path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_entry(metadata, body))
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        if must_not_exist and path.exists():
            raise Conflict(f"Entry path already exists: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relocate_entry(
    source: Path,
    destination: Path,
    metadata: dict[str, object],
    body: str,
) -> None:
    """Move one entry to a new path while rewriting its frontmatter.

    The new content is written over the source first and the file is then
    renamed, so an interrupted move can never leave the same entry ID at two
    paths. Callers must hold the exclusive vault lock and must have rejected an
    existing destination, because ``os.replace`` would overwrite it.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_entry(source, metadata, body)
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Separate filesystems cannot be renamed across. Fall back to the
        # copy-then-delete sequence, which is briefly non-atomic.
        atomic_write_entry(destination, metadata, body, must_not_exist=True)
        source.unlink()


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed


def safe_restore_path(root: Path, value: object) -> Path:
    if not isinstance(value, str):
        raise ValidationError("deleted_from must be a relative path below vault/")
    relative = Path(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "vault"
    ):
        raise ValidationError("deleted_from must be a relative path below vault/")
    destination = (root / relative).resolve()
    vault = (root / "vault").resolve()
    if not destination.is_relative_to(vault):
        raise ValidationError("deleted_from resolves outside vault/")
    return destination

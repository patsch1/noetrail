#!/usr/bin/env python3
"""Writing a program file outside the instance, the way `skills` and `connect` do.

`skills export` copies skills into a host's directory and `connect --write`
merges a server entry into a host's configuration file. Neither writes inside
a vault, so neither goes through the store's entry writer, but both need the
same two guarantees: a reader never sees half a file, and nothing lands in a
Noetrail data or configuration root, where a program file would sit among
private data.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import os
from pathlib import Path
import tempfile

from noetrail.layout import (
    default_config_root,
    environment_config_root,
    environment_data_root,
)


def protected_roots(
    args: argparse.Namespace, extra: Iterable[Path | None] = ()
) -> list[Path]:
    """The roots a program file must never be written into, resolved.

    The root options on ``args``, the root environment variables and the
    default configuration root, plus whatever the caller adds (``connect``
    adds the instance it resolved, which may be the quickstart default no
    option names).
    """

    candidates: list[str | Path | None] = [
        getattr(args, "root", None),
        getattr(args, "data_root", None),
        getattr(args, "config_root", None),
        environment_data_root(),
        environment_config_root(),
        default_config_root(),
        *extra,
    ]
    return [
        Path(value).expanduser().resolve()
        for value in candidates
        if value is not None
    ]


def atomic_write(destination: Path, content: bytes) -> None:
    """Replace ``destination`` with ``content`` in one rename.

    The bytes go to a temporary file in the same directory, are flushed to
    disk, and are renamed over the destination, so a crash leaves either the
    old file or the new one. Like every other atomic write here, a replaced
    file keeps its mode, and a new one keeps mkstemp's owner-only mode.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists():
            os.chmod(temporary, destination.stat().st_mode & 0o777)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

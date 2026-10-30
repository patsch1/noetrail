#!/usr/bin/env python3
"""`connect --write`: merge the server entry into a JSON host's own file.

Pasting the printed block is the last manual step between `quickstart` and a
working host, and the step where a reader is most likely to break something:
the block has to go inside an object that usually holds other servers, under
the right key, without a stray comma. For the hosts whose configuration is a
JSON file, `connect --write` does that merge itself.

It is a dry run unless `--apply` is given, like `import` and `merge`: the
preview names the file, whether it would be created or updated, the backup it
would make, and the entry it would write. With `--apply` it performs exactly
that change and nothing else. Everything else in the file is kept as parsed;
an entry of the same name that differs is replaced only with `--force`.

What it refuses, because each would mean guessing on someone else's file:

- a TOML host (the standard library reads TOML but cannot write it, and
  rewriting a hand-kept TOML file by hand is not something this tool does);
- a file that is not plain JSON, such as JSONC with comments, which a
  rewrite would silently strip;
- a symbolic link anywhere below the directory the path starts from, since
  dotfile repositories link these files and the write would land in the
  repository instead;
- a host directory that does not exist, which usually means the host is not
  installed here, unless the path was given with `--config-file`;
- any path inside a Noetrail data, configuration, built-ins, or skills root.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from noetrail.commands.safe_write import atomic_write, protected_roots
from noetrail.errors import Conflict, InvalidRequest, NoetrailError, StorageError
from noetrail.layout import LayoutError, NoetrailLayout, default_skills_root

if TYPE_CHECKING:
    from noetrail.commands.connect import Host

# A leading `%NAME%`, `${NAME}` or `$NAME`, as a configuration path from the
# host table may start with one.
ENVIRONMENT_REFERENCE = re.compile(
    r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)
BACKUP_STAMP = "%Y%m%dT%H%M%SZ"
APPLY_HINT = "Nothing was written. Run the same command with --apply to write it."


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Target:
    """Where to write, and the directory below which no link is accepted."""

    path: Path
    anchor: Path
    create_parents: bool


@dataclass
class WritePlan:
    """What `--write` would do to one file, and after `--apply` what it did."""

    path: Path
    entry_label: str
    entry: dict[str, object]
    action: str = "refuse"
    reason: str | None = None
    error: NoetrailError | None = None
    existing: object = None
    replaces: bool = False
    original: bytes | None = None
    content: bytes | None = None
    backup: Path | None = None
    stamp: str | None = None
    applied: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "action": self.action,
            "backup": None if self.backup is None else str(self.backup),
            "applied": self.applied,
            "reason": self.reason,
        }


def default_target(
    host: Host,
    raw: str | None,
    *,
    platform: str,
    environ: Mapping[str, str],
    cwd: Path,
) -> Target:
    """The host's printed configuration path, expanded for this machine.

    ``~`` is the home directory and a leading environment reference is that
    variable's value; either is the anchor no link may appear below. A
    relative path is the host's project file and resolves against ``cwd``,
    where creating its directory (`.vscode/`) is fine.
    """

    if raw is None:
        raise InvalidRequest(
            f"{host.display_name} has no documented configuration file on this "
            "platform; pass --config-file to choose a path"
        )
    text = raw.replace("\\", "/") if platform == "windows" else raw
    reference = ENVIRONMENT_REFERENCE.match(text)
    if text.startswith("~"):
        anchor = Path(os.path.expanduser("~"))
        rest = text[1:]
    elif reference is not None:
        name = next(group for group in reference.groups() if group is not None)
        value = environ.get(name)
        if not value:
            raise InvalidRequest(
                f"{name} is not set, so {host.display_name}'s configuration "
                "file cannot be located; pass --config-file to choose a path"
            )
        anchor = Path(os.path.normpath(cwd / value))
        rest = text[reference.end() :]
    else:
        return Target(cwd / text, cwd, create_parents=True)
    return Target(anchor / rest.lstrip("/"), anchor, create_parents=False)


def explicit_target(value: str, *, cwd: Path) -> Target:
    """A `--config-file` path: parents may be created, and no link is followed.

    Below the home directory for a ``~`` path, otherwise below the filesystem
    root, so the escape hatch for a linked file is its real path.
    """

    path = Path(os.path.normpath(cwd / os.path.expanduser(value)))
    home = Path(os.path.expanduser("~"))
    anchor = home if path.is_relative_to(home) else Path(path.anchor)
    return Target(path, anchor, create_parents=True)


def check_links(target: Target) -> None:
    current = target.anchor
    for part in target.path.relative_to(target.anchor).parts:
        current = current / part
        if os.path.islink(current):
            raise InvalidRequest(
                f"Refusing to write through a symbolic link: {current}. A "
                "linked configuration file usually lives in a dotfile "
                "repository, and the write would land there. Pass "
                f"--config-file {os.path.realpath(target.path)} to write the "
                "real file."
            )
        if not os.path.lexists(current):
            return


def check_roots(path: Path, roots: list[Path]) -> None:
    resolved = Path(os.path.realpath(path))
    for root in roots:
        if resolved.is_relative_to(root):
            raise InvalidRequest(
                "Refusing to write a host configuration inside a Noetrail "
                f"data, configuration, built-ins, or skills root: {resolved}"
            )


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateKey(key)
        document[key] = value
    return document


def read_document(path: Path, original: bytes) -> dict[str, object]:
    """The file as a JSON object, or a refusal saying why it cannot be kept."""

    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidRequest(f"{path} is not UTF-8 text") from exc
    if not text.strip():
        return {}
    try:
        document = json.loads(text, object_pairs_hook=_unique_object)
    except _DuplicateKey as exc:
        raise InvalidRequest(
            f"{path} repeats the key {exc.args[0]!r}; a rewrite would keep "
            "only the last one, so paste the printed block yourself"
        ) from exc
    except json.JSONDecodeError as exc:
        raise InvalidRequest(
            f"{path} is not plain JSON ({exc.msg} at line {exc.lineno}). "
            "Comments and trailing commas, which JSONC allows, would be lost "
            "in a rewrite, so paste the printed block into it yourself"
        ) from exc
    if not isinstance(document, dict):
        raise InvalidRequest(f"{path} does not hold a JSON object")
    return document


def detect_indent(text: str) -> int | str:
    """Four spaces or a tab when the file already uses them, else two."""

    match = re.search(r"\n([ \t]+)\S", text)
    if match is None:
        return 2
    leading = match.group(1)
    if leading.startswith("\t"):
        return "\t"
    return 4 if len(leading) == 4 else 2


def _canonical(value: object) -> str:
    # `True == 1` in Python; the serialized form tells them apart.
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def backup_name(path: Path, stamp: str, counter: int) -> Path:
    """``<file>.<stamp>.bak``, then ``<file>.<stamp>-1.bak`` and so on."""

    suffix = "" if counter == 0 else f"-{counter}"
    return path.with_name(f"{path.name}.{stamp}{suffix}.bak")


def merge(
    host: Host,
    plan: WritePlan,
    document: dict[str, object] | None,
    *,
    force: bool,
) -> None:
    """Decide the action and the new bytes; ``document`` is None for a new file."""

    key, name = host.top_level_key, host.server_name
    if document is None:
        merged: dict[str, object] = {}
        if host.json_schema is not None:
            merged["$schema"] = host.json_schema
        servers: dict[str, object] = {}
        indent: int | str = 2
        plan.action = "create"
    else:
        current = document.get(key, {})
        if not isinstance(current, dict):
            raise InvalidRequest(
                f"{key} in {plan.path} is not a JSON object, so the server "
                "cannot be added to it"
            )
        merged, servers = dict(document), dict(current)
        assert plan.original is not None
        indent = detect_indent(plan.original.decode("utf-8"))
        plan.action = "update"
        if name in servers:
            plan.existing = servers[name]
            if _canonical(servers[name]) == _canonical(plan.entry):
                plan.action = "unchanged"
                plan.reason = f"{plan.entry_label} already holds this entry"
                return
            if not force:
                raise Conflict(
                    f"{plan.entry_label} in {plan.path} already holds a "
                    "different server definition; pass --force to replace it "
                    "(the file is backed up first)"
                )
            plan.replaces = True
            plan.reason = f"replaces the existing {plan.entry_label} (--force)"
    servers[name] = plan.entry
    merged[key] = servers
    rendered = json.dumps(merged, indent=indent, ensure_ascii=False) + "\n"
    plan.content = rendered.encode("utf-8")


def protected(args: argparse.Namespace, layout: NoetrailLayout) -> list[Path]:
    try:
        skills_root: Path | None = default_skills_root()
    except LayoutError:
        skills_root = None
    return protected_roots(
        args,
        extra=(
            layout.data_root,
            layout.config_root,
            layout.compatibility_root,
            layout.builtins_root,
            skills_root,
        ),
    )


def plan_write(
    host: Host,
    report: Mapping[str, object],
    layout: NoetrailLayout,
    args: argparse.Namespace,
    *,
    platform: str,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
) -> WritePlan:
    """Everything `--write` would do, decided without writing.

    A refusal about the request itself (a TOML host, no path on this
    platform) is raised. One about the target file is recorded in the plan,
    so the preview can show it, and the caller raises it after printing.
    """

    if host.config_format != "json":
        raise InvalidRequest(
            f"{host.display_name} keeps its configuration in TOML, which "
            "connect only prints: the standard library reads TOML but cannot "
            "write it, and rewriting someone's TOML by hand is not something "
            f"this tool does. Paste the printed block into {report['config_path']} "
            "yourself"
        )
    if args.config_file is not None:
        target = explicit_target(args.config_file, cwd=cwd)
    else:
        raw = report["config_path"]
        target = default_target(
            host,
            None if raw is None else str(raw),
            platform=platform,
            environ=os.environ if environ is None else environ,
            cwd=cwd,
        )
    entry = report["server"]
    assert isinstance(entry, dict)
    plan = WritePlan(
        path=target.path,
        entry_label=f"{host.top_level_key}.{host.server_name}",
        entry=entry,
    )
    try:
        _inspect(host, plan, target, protected(args, layout), force=args.force)
    except (InvalidRequest, Conflict, StorageError) as exc:
        plan.action = "refuse"
        plan.reason = exc.message
        plan.error = exc
        plan.backup = None
    return plan


def _inspect(
    host: Host,
    plan: WritePlan,
    target: Target,
    roots: list[Path],
    *,
    force: bool,
) -> None:
    check_links(target)
    check_roots(target.path, roots)
    parent = target.path.parent
    if not parent.is_dir():
        if os.path.lexists(parent):
            raise InvalidRequest(f"Not a directory: {parent}")
        if not target.create_parents:
            raise InvalidRequest(
                f"{host.display_name} does not look installed here: {parent} "
                "does not exist. Pass --config-file to choose a path"
            )
    if os.path.lexists(target.path):
        if not target.path.is_file():
            raise InvalidRequest(f"Not a regular file: {target.path}")
        try:
            plan.original = target.path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Could not read {target.path}: {exc}") from exc
        document: dict[str, object] | None = read_document(
            target.path, plan.original
        )
    else:
        document = None
    merge(host, plan, document, force=force)
    if plan.original is not None and plan.action != "unchanged":
        plan.stamp = utc_now().strftime(BACKUP_STAMP)
        counter = 0
        while os.path.lexists(backup_name(target.path, plan.stamp, counter)):
            counter += 1
        plan.backup = backup_name(target.path, plan.stamp, counter)


def write_backup(plan: WritePlan) -> Path:
    """Copy the original next to it, never over an existing file.

    Owner-only, whatever the original's mode: a host configuration can hold
    tokens in other servers' environment, and a backup is easy to forget.
    """

    assert plan.original is not None and plan.stamp is not None
    counter = 0
    while True:
        candidate = backup_name(plan.path, plan.stamp, counter)
        counter += 1
        try:
            descriptor = os.open(
                candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plan.original)
            handle.flush()
            os.fsync(handle.fileno())
        return candidate


def apply(plan: WritePlan) -> None:
    """Perform exactly the planned change; a refused or unchanged plan is a no-op."""

    if plan.action not in ("create", "update"):
        return
    assert plan.content is not None
    try:
        current = plan.path.read_bytes() if os.path.lexists(plan.path) else None
        if current != plan.original:
            raise Conflict(
                f"{plan.path} changed while connect was reading it; run the "
                "command again"
            )
        if plan.original is not None:
            plan.backup = write_backup(plan)
        atomic_write(plan.path, plan.content)
    except OSError as exc:
        raise StorageError(f"Could not write {plan.path}: {exc}") from exc
    plan.applied = True


def _entry_block(label: str, value: object) -> list[str]:
    return [f"{label}:", json.dumps(value, ensure_ascii=False, indent=2)]


def render_plan(host: Host, plan: WritePlan, report: Mapping[str, object]) -> str:
    """The human form of a write preview or of what `--apply` did."""

    if plan.applied:
        state = "written"
    elif plan.action == "refuse":
        state = "refused"
    elif plan.action == "unchanged":
        state = "nothing to write"
    else:
        state = "preview, nothing written yet"
    heading = f"{host.display_name} ({host.name}): {state}"
    action = plan.action
    if plan.action == "create":
        action = f"create a new file with {plan.entry_label}"
    elif plan.action == "update":
        action = (
            f"update: {plan.reason}"
            if plan.replaces
            else f"update: add {plan.entry_label}, keeping everything else"
        )
    elif plan.action == "unchanged":
        action = f"unchanged: {plan.reason}"
    if plan.backup is not None:
        backup = str(plan.backup)
    elif plan.action == "create":
        backup = "none (new file)"
    else:
        backup = "none"
    lines = [
        heading,
        f"  file      {plan.path}",
        f"  action    {action}",
        f"  backup    {backup}",
        "",
    ]
    if plan.existing is not None and plan.action != "unchanged":
        lines += _entry_block(f"Existing {plan.entry_label}", plan.existing)
        lines += [""]
        lines += _entry_block(f"New {plan.entry_label}", plan.entry)
    else:
        lines += _entry_block(plan.entry_label, plan.entry)
    if plan.action in ("create", "update"):
        if not plan.applied:
            lines += ["", APPLY_HINT]
        else:
            if report["skills_export"] is not None:
                lines += ["", "Copy the skills with:", f"  {report['skills_export']}"]
            if host.notes:
                lines += ["", *host.notes]
    return "\n".join(lines)

#!/usr/bin/env python3
"""Resolve trusted application, configuration, and private data roots."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sysconfig

DATA_ROOT_ENV = "NOETRAIL_DATA_ROOT"
CONFIG_ROOT_ENV = "NOETRAIL_CONFIG_ROOT"
BUILTINS_ROOT_ENV = "NOETRAIL_BUILTINS_ROOT"
LEGACY_DATA_ROOT_ENV = "KNOWLEDGE_DATA_ROOT"
LEGACY_CONFIG_ROOT_ENV = "KNOWLEDGE_CONFIG_ROOT"
LEGACY_BUILTINS_ROOT_ENV = "KNOWLEDGE_BUILTINS_ROOT"


def _environment_value(primary: str, legacy: str) -> str | None:
    """Prefer the Noetrail variable while accepting the pre-rename alias."""

    return os.environ.get(primary) or os.environ.get(legacy)


def environment_data_root() -> str | None:
    return _environment_value(DATA_ROOT_ENV, LEGACY_DATA_ROOT_ENV)


def environment_config_root() -> str | None:
    return _environment_value(CONFIG_ROOT_ENV, LEGACY_CONFIG_ROOT_ENV)


def environment_builtins_root() -> str | None:
    return _environment_value(BUILTINS_ROOT_ENV, LEGACY_BUILTINS_ROOT_ENV)


class LayoutError(ValueError):
    """A requested knowledge layout is missing, ambiguous, or unsafe."""


def _resolve_existing_directory(value: str | Path, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise LayoutError(f"{label} is unavailable: {value}") from exc
    if not path.is_dir():
        raise LayoutError(f"{label} is not a directory: {path}")
    return path


def _resolve_optional_directory(value: str | Path, label: str) -> Path:
    raw = Path(value).expanduser()
    try:
        path = raw.resolve(strict=raw.exists() or raw.is_symlink())
    except OSError as exc:
        raise LayoutError(f"{label} is unavailable: {value}") from exc
    if path.exists() and not path.is_dir():
        raise LayoutError(f"{label} is not a directory: {path}")
    return path


def _validate_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise LayoutError(f"{label} must not be a symbolic link: {path}")
    if not path.is_file():
        raise LayoutError(f"{label} does not exist: {path}")
    return path


def _validate_optional_child(
    root: Path,
    relative: str,
    *,
    directory: bool,
    label: str,
) -> Path:
    candidate = root / relative
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    if candidate.is_symlink():
        raise LayoutError(
            f"{label} {relative} must not be a symbolic link"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LayoutError(
            f"{label} {relative} is unavailable"
        ) from exc
    if not resolved.is_relative_to(root):
        raise LayoutError(
            f"{label} {relative} resolves outside its root"
        )
    if directory and not resolved.is_dir():
        raise LayoutError(
            f"{label} {relative} is not a directory"
        )
    if not directory and not resolved.is_file():
        raise LayoutError(f"{label} {relative} is not a file")
    return resolved


def default_application_root() -> Path:
    """Return the immutable source or installed-package resource root."""

    for source_root in Path(__file__).resolve().parents[1:4]:
        if (source_root / ".knowledge" / "SPEC.md").is_file():
            return source_root
    return Path(sysconfig.get_path("data")).resolve()


def default_builtins_root(application_root: Path) -> Path:
    """Locate bundled resources in a checkout or an installed distribution."""

    source_candidate = application_root / ".knowledge"
    if (source_candidate / "SPEC.md").is_file():
        return source_candidate
    canonical = (
        application_root
        / "share"
        / "noetrail"
        / ".knowledge"
    )
    if (canonical / "SPEC.md").is_file():
        return canonical
    legacy = (
        application_root
        / "share"
        / "personal-knowledge-vault"
        / ".knowledge"
    )
    if (legacy / "SPEC.md").is_file():
        return legacy
    return canonical


def default_config_root() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    canonical = base / "noetrail"
    legacy = base / "knowledge"
    if not canonical.exists() and legacy.is_dir():
        return legacy
    return canonical


@dataclass(frozen=True)
class NoetrailLayout:
    """Resolved roots used by one CLI or MCP process."""

    application_root: Path
    builtins_root: Path
    config_root: Path
    data_root: Path
    compatibility_root: Path | None = None

    @property
    def vault_root(self) -> Path:
        return self.data_root / "vault"

    @property
    def trash_root(self) -> Path:
        return self.data_root / "trash"

    @property
    def imports_root(self) -> Path:
        return self.data_root / "imports"

    @property
    def local_packs_root(self) -> Path:
        return _validate_optional_child(
            self.config_root,
            "packs",
            directory=True,
            label="instance configuration",
        )

    @property
    def builtins_packs_root(self) -> Path:
        return _validate_optional_child(
            self.builtins_root,
            "packs",
            directory=True,
            label="built-in resource",
        )

    @property
    def specification_path(self) -> Path:
        return _validate_regular_file(
            self.builtins_root / "SPEC.md",
            "built-in vault specification",
        )

    @property
    def relation_types_path(self) -> Path:
        local = _validate_optional_child(
            self.config_root,
            "relation-types.yaml",
            directory=False,
            label="instance configuration",
        )
        if local.is_file():
            return local
        return _validate_regular_file(
            self.builtins_root / "relation-types.yaml",
            "built-in relation definitions",
        )

    @property
    def views_path(self) -> Path:
        return _validate_optional_child(
            self.config_root,
            "views.yaml",
            directory=False,
            label="instance configuration",
        )

    def validate_paths(self) -> None:
        """Resolve every lazily validated path so errors surface at startup.

        The properties below raise :class:`LayoutError` on symlinks, escapes,
        and missing resources. Touching them eagerly keeps a misconfigured
        instance from failing halfway through an operation.
        """

        _ = (
            self.specification_path,
            self.relation_types_path,
            self.local_packs_root,
            self.views_path,
        )

    def cli_root_arguments(self) -> list[str]:
        """Return arguments that reproduce this layout in a child CLI."""

        if self.compatibility_root is not None:
            return ["--root", str(self.compatibility_root)]
        return [
            "--data-root",
            str(self.data_root),
            "--config-root",
            str(self.config_root),
            "--builtins-root",
            str(self.builtins_root),
        ]


def _legacy_root(start: Path, application_root: Path) -> Path | None:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents, application_root):
        if (candidate / ".knowledge" / "SPEC.md").is_file():
            return candidate
    return None


def resolve_layout(
    *,
    root: str | Path | None = None,
    data_root: str | Path | None = None,
    config_root: str | Path | None = None,
    builtins_root: str | Path | None = None,
    application_root: str | Path | None = None,
    start: str | Path | None = None,
) -> NoetrailLayout:
    """Resolve either the legacy combined root or the separated layout."""

    application = _resolve_existing_directory(
        application_root or default_application_root(),
        "application root",
    )
    if root is not None:
        if any(value is not None for value in (data_root, config_root, builtins_root)):
            raise LayoutError(
                "--root cannot be combined with --data-root, --config-root, "
                "or --builtins-root"
            )
        combined = _resolve_existing_directory(root, "knowledge root")
        resources = _resolve_existing_directory(
            combined / ".knowledge",
            "combined .knowledge root",
        )
        layout = NoetrailLayout(
            application_root=application,
            builtins_root=resources,
            config_root=resources,
            data_root=combined,
            compatibility_root=combined,
        )
        layout.validate_paths()
        return layout

    explicit_separated = any(
        value is not None for value in (data_root, config_root, builtins_root)
    )
    environment_data = environment_data_root()
    environment_config = environment_config_root()
    environment_builtins = environment_builtins_root()
    separated_requested = explicit_separated or any(
        value is not None
        for value in (environment_data, environment_config, environment_builtins)
    )

    if not separated_requested:
        legacy = _legacy_root(
            Path(start) if start is not None else Path.cwd(),
            application,
        )
        if legacy is None:
            raise LayoutError(
                "Could not find a combined knowledge root; pass --data-root "
                "for a separated installation"
            )
        return resolve_layout(root=legacy, application_root=application)

    selected_data = data_root or environment_data
    if selected_data is None:
        raise LayoutError(
            "Separated layout requires --data-root or NOETRAIL_DATA_ROOT "
            "(legacy: KNOWLEDGE_DATA_ROOT)"
        )
    data = _resolve_existing_directory(selected_data, "data root")
    builtins = _resolve_existing_directory(
        builtins_root
        or environment_builtins
        or default_builtins_root(application),
        "built-ins root",
    )
    config = _resolve_optional_directory(
        config_root or environment_config or default_config_root(),
        "instance config root",
    )
    if data == builtins or data == config or builtins == config:
        raise LayoutError(
            "Separated data, config, and built-ins roots must be distinct"
        )

    layout = NoetrailLayout(
        application_root=application,
        builtins_root=builtins,
        config_root=config,
        data_root=data,
    )
    layout.validate_paths()
    return layout

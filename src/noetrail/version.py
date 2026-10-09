"""One dependency-free application version source for runtime surfaces."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION_NAME = "noetrail"
LEGACY_DISTRIBUTION_NAME = "personal-knowledge-vault"
FALLBACK_VERSION = "0.10.0a5"


def application_version() -> str:
    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        try:
            return version(LEGACY_DISTRIBUTION_NAME)
        except PackageNotFoundError:
            return FALLBACK_VERSION

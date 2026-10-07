#!/usr/bin/env python3
"""The failure vocabulary every Noetrail operation raises.

Before this module the package reported every refusal by raising
``SystemExit`` with a sentence in it. That made the CLI the only usable
interface: a caller inside the same process had to catch an interpreter
shutdown signal and then guess what had gone wrong by matching prose. The MCP
server did exactly that, and ``validate_attachment_blob`` caught ``SystemExit``
around a magic-byte check to turn "not an image" back into a validation line.

Every raise now carries a stable ``code`` next to the human sentence. The CLI
still prints the sentence to stderr and exits with ``exit_code``, so nothing a
shell script or a test observes changed; the MCP server reports the code as a
machine-readable field, so an agent can branch on ``revision_conflict`` without
reading English.

``SystemExit`` keeps exactly one job: what ``argparse`` raises for ``--help``
and for a usage error. Those are not vault failures and are not translated.
"""

from __future__ import annotations


class NoetrailError(Exception):
    """A refusal a caller is meant to see, with a stable machine code."""

    code = "error"
    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def as_dict(self) -> dict[str, str]:
        """The wire shape shared by the MCP server and any other caller."""

        return {"code": self.code, "message": self.message}


class InvalidRequest(NoetrailError):
    """Arguments or a payload the caller supplied do not make sense."""

    code = "invalid_request"


class NotFound(NoetrailError):
    """The addressed entry, relation, or reference does not exist."""

    code = "not_found"


class Conflict(NoetrailError):
    """The vault already holds something the request would collide with."""

    code = "conflict"


class RevisionConflict(Conflict):
    """The entry changed between the read and the write."""

    code = "revision_conflict"


class NoChange(NoetrailError):
    """The request is well-formed but would leave the entry as it is.

    Separate from ``Conflict`` because it is the one refusal an agent can
    safely treat as success: the vault already holds the requested state.
    """

    code = "no_change"


class ValidationError(NoetrailError):
    """Stored entry content fails the invariants ``validate`` checks."""

    code = "validation_error"


class UnsupportedSchema(NoetrailError):
    """The entry uses a schema or type version this build cannot edit."""

    code = "unsupported_schema"


class VaultBusy(NoetrailError):
    """Another process holds the vault lock."""

    code = "vault_busy"


class StorageError(NoetrailError):
    """The filesystem below the vault is missing, unreadable, or unsafe."""

    code = "storage_error"


class ConfigurationError(NoetrailError):
    """The installation -- roots, configuration file, or schema pack -- is bad."""

    code = "configuration_error"


class InternalError(NoetrailError):
    """An invariant inside Noetrail broke; the caller cannot fix this."""

    code = "internal_error"

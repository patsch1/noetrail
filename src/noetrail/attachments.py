#!/usr/bin/env python3
"""Inbox reads, blob writes, image detection, blob validation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from noetrail.constants import ATTACHMENT_PATH_PATTERN, MAX_ATTACHMENT_BYTES
from noetrail.errors import (
    InvalidRequest,
    NotFound,
    StorageError,
    ValidationError,
)


def detect_attachment_image(data: bytes) -> tuple[str, str] | None:
    """Return a canonical MIME type and extension from image magic bytes.

    ``None`` means "these bytes are not one of the supported images". The
    function used to raise instead, which forced every caller that merely
    wanted to *ask* -- the blob validator, and the MCP server's inbox listing
    -- to catch an exception around a byte comparison.
    """

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brands = {data[8:12]}
        brands.update(
            data[index : index + 4]
            for index in range(16, min(len(data), 64), 4)
            if len(data[index : index + 4]) == 4
        )
        if brands & {b"avif", b"avis"}:
            return "image/avif", "avif"
        if brands & {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"heim",
            b"heis",
            b"mif1",
            b"msf1",
        }:
            return "image/heic", "heic"
    return None


def require_attachment_image(data: bytes) -> tuple[str, str]:
    """``detect_attachment_image`` for callers that need a supported image."""

    detected = detect_attachment_image(data)
    if detected is None:
        raise InvalidRequest(
            "Attachment is not a supported JPEG, PNG, WebP, GIF, HEIC, or AVIF image"
        )
    return detected


def attachment_inbox_path(value: str | None) -> Path:
    if value is None:
        raise InvalidRequest(
            "Attachment inbox is not configured; start the tool with "
            "--attachment-inbox"
        )
    try:
        inbox = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise StorageError("Attachment inbox is unavailable") from exc
    if not inbox.is_dir():
        raise InvalidRequest("Attachment inbox is not a directory")
    return inbox


def normalize_attachment_name(value: str, extension: str) -> str:
    name = re.sub(r"^[0-9a-fA-F]{32}_", "", Path(value).name)
    name = "".join(
        character if character >= " " and character != "\x7f" else "_"
        for character in name
    ).strip()
    return (name[:255] if name else f"image.{extension}")


def read_attachment_source(
    source_value: str,
    inbox: Path,
) -> tuple[bytes, str, str, str]:
    if not source_value or "\x00" in source_value:
        raise InvalidRequest("Attachment source path is invalid")
    source = Path(source_value).expanduser()
    candidate = source if source.is_absolute() else inbox / source
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(inbox)
    except (OSError, ValueError) as exc:
        raise InvalidRequest(
            "Attachment source is outside the configured inbox"
        ) from exc
    if candidate.is_symlink():
        raise InvalidRequest("Attachment source must not be a symbolic link")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise StorageError("Attachment source could not be opened") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise InvalidRequest("Attachment source must be a regular file")
        if details.st_size < 1:
            raise InvalidRequest("Attachment source is empty")
        if details.st_size > MAX_ATTACHMENT_BYTES:
            raise InvalidRequest(
                f"Attachment exceeds the {MAX_ATTACHMENT_BYTES}-byte limit"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(MAX_ATTACHMENT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise InvalidRequest(
            f"Attachment exceeds the {MAX_ATTACHMENT_BYTES}-byte limit"
        )
    media_type, extension = require_attachment_image(data)
    original_name = normalize_attachment_name(resolved.name, extension)
    return data, original_name, media_type, extension


def attachment_blob_path(root: Path, digest: str, extension: str) -> Path:
    return (
        root
        / "vault"
        / "attachments"
        / digest[:2]
        / f"{digest}.{extension}"
    )


def write_attachment_blob(
    root: Path,
    data: bytes,
    digest: str,
    extension: str,
) -> Path:
    vault_root = root / "vault"
    vault_root.mkdir(exist_ok=True)
    if vault_root.is_symlink() or not vault_root.is_dir():
        raise StorageError("Vault root is not a regular directory")
    storage_root = vault_root / "attachments"
    storage_root.mkdir(exist_ok=True)
    if storage_root.is_symlink() or not storage_root.is_dir():
        raise StorageError("Attachment storage root is not a regular directory")
    destination = attachment_blob_path(root, digest, extension)
    destination.parent.mkdir(exist_ok=True)
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise StorageError("Attachment storage shard is not a regular directory")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise StorageError(
                "Attachment destination is not a regular non-symlink file"
            )
        if destination.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise StorageError("Existing attachment blob exceeds the size limit")
        existing = destination.read_bytes()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise StorageError("Attachment storage hash collision")
        return destination

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{digest}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def validate_attachment_blob(
    root: Path,
    attachment: dict[str, object],
) -> list[str]:
    errors: list[str] = []
    relative_value = attachment.get("path")
    if not isinstance(relative_value, str) or not ATTACHMENT_PATH_PATTERN.fullmatch(
        relative_value
    ):
        return errors
    relative = Path(relative_value)
    candidate = root / relative
    attachment_root = root / "vault" / "attachments"
    try:
        resolved_root = attachment_root.resolve()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return ["file is missing or outside vault/attachments"]
    if candidate.is_symlink() or not resolved.is_file():
        return ["file is not a regular non-symlink file"]
    try:
        size = resolved.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            return [f"file exceeds the {MAX_ATTACHMENT_BYTES}-byte limit"]
        data = resolved.read_bytes()
    except OSError:
        return ["file could not be read"]
    if size != attachment.get("size_bytes"):
        errors.append("file size does not match size_bytes")
    digest = hashlib.sha256(data).hexdigest()
    if digest != attachment.get("sha256"):
        errors.append("file hash does not match sha256")
    detected = detect_attachment_image(data)
    if detected is None:
        errors.append("file is not a supported image")
    else:
        media_type, extension = detected
        if media_type != attachment.get("media_type"):
            errors.append("file content does not match media_type")
        if resolved.suffix != f".{extension}":
            errors.append("file extension does not match image content")
    return errors


def read_attachment_blob(
    root: Path,
    attachment: dict[str, object],
    *,
    max_bytes: int,
) -> tuple[bytes, str]:
    """Return the bytes of one recorded attachment, or refuse to.

    Storing a photo the user can never see again is a poor bargain, so a
    caller may ask for the content back. What it may *not* do is name a file:
    the only way in is an attachment record that an entry already holds, and
    every field of that record is checked against the file before a byte is
    returned.

    The checks are `validate_attachment_blob`'s, applied as preconditions
    rather than reported as findings -- the path shape, the resolved location
    inside `vault/attachments`, no symlink, the recorded size and digest, and
    the magic bytes agreeing with the recorded media type. A blob that fails
    any of them is a blob whose identity is not what the entry claims, and
    handing those bytes to a chat channel is exactly when that matters.
    """

    relative_value = attachment.get("path")
    if not isinstance(relative_value, str) or not ATTACHMENT_PATH_PATTERN.fullmatch(
        relative_value
    ):
        raise ValidationError("Attachment record has no usable path")

    candidate = root / Path(relative_value)
    attachment_root = root / "vault" / "attachments"
    try:
        resolved_root = attachment_root.resolve()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise NotFound(
            "Attachment file is missing or outside vault/attachments"
        ) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise ValidationError("Attachment is not a regular non-symlink file")

    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise NotFound("Attachment file could not be read") from exc
    # Bounded before the read, not after: the point of a limit is not to
    # measure the file but to avoid holding it.
    if size > max_bytes:
        raise InvalidRequest(
            f"Attachment is {size} bytes and this tool delivers at most "
            f"{max_bytes}; it remains stored and can be read from the vault "
            "directly"
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise NotFound("Attachment file could not be read") from exc

    if size != attachment.get("size_bytes"):
        raise ValidationError("Attachment size does not match its record")
    if hashlib.sha256(data).hexdigest() != attachment.get("sha256"):
        raise ValidationError("Attachment content does not match its record")
    detected = detect_attachment_image(data)
    if detected is None:
        raise ValidationError("Attachment is not a supported image")
    media_type, _extension = detected
    if media_type != attachment.get("media_type"):
        raise ValidationError("Attachment content does not match its media type")
    return data, media_type

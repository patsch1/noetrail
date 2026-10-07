#!/usr/bin/env python3
"""Reading the JSON payload files the commands accept."""

from __future__ import annotations

import json
from pathlib import Path

from noetrail.constants import BOOKMARK_PAYLOAD_KEYS, UPDATE_PAYLOAD_KEYS
from noetrail.errors import (
    InvalidRequest,
)


def load_bookmark_payload(path_value: str | None) -> dict[str, object]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidRequest(f"Could not read bookmark metadata file: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("Bookmark metadata file must contain one JSON object")
    unknown = sorted(set(payload) - BOOKMARK_PAYLOAD_KEYS)
    if unknown:
        raise InvalidRequest(f"Unknown bookmark metadata fields: {', '.join(unknown)}")
    return payload


def load_attributes_payload(path_value: str | None) -> dict[str, object]:
    if path_value is None:
        return {}
    path = Path(path_value).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidRequest(f"Could not read attributes file: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("Attributes file must contain one JSON object")
    return payload


def load_update_payload(path_value: str) -> dict[str, object]:
    path = Path(path_value).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidRequest(f"Could not read update patch file: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("Update patch file must contain one JSON object")
    unknown = sorted(set(payload) - UPDATE_PAYLOAD_KEYS)
    if unknown:
        raise InvalidRequest(f"Unknown update fields: {', '.join(unknown)}")
    if not payload:
        raise InvalidRequest("Update patch is empty")
    return payload

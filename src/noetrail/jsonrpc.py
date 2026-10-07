# SPDX-License-Identifier: Apache-2.0
"""Bounded JSON parsing for the stdio and HTTP JSON-RPC transports.

``json.loads`` parses containers recursively, so deeply nested input raises
``RecursionError`` rather than ``json.JSONDecodeError``. Both servers used to
catch only the latter, which turned a single malformed request into a process
exit on the stdio transport and into a full traceback in the log on the HTTP
transport.

This module rejects over-nested documents before the parser ever sees them: a
linear scan over the raw text tracks bracket depth outside of string literals,
which costs one pass and cannot itself recurse. ``RecursionError`` is still
caught as a defence in depth in case a future interpreter nests differently.

The module is deliberately dependency-free and imports nothing from the rest of
the package, so the bookmark fetcher image can vendor it without pulling in any
vault code.
"""

from __future__ import annotations

import json

MAX_JSON_DEPTH = 64
"""Maximum nesting depth accepted from a peer.

The deepest structure any tool schema describes is a list of relation objects
inside a capture payload, which reaches depth 4. 64 leaves a very wide margin
while staying far below CPython's default recursion limit.
"""


class JsonRequestError(ValueError):
    """Raised when a peer sends a document this server refuses to parse."""


def scan_depth(text: str) -> int:
    """Return the maximum bracket nesting depth of ``text``.

    Brackets inside string literals are ignored, and backslash escapes are
    honoured, so ``'{"a": "]]]]"}'`` reports depth 1. Unbalanced input is not
    an error here; ``json.loads`` reports that with a precise message.
    """
    depth = 0
    maximum = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                maximum = depth
        elif character in "]}":
            depth -= 1
    return maximum


def loads_bounded(data: str | bytes, *, max_depth: int = MAX_JSON_DEPTH) -> object:
    """Parse ``data`` as JSON, refusing documents nested deeper than ``max_depth``.

    Raises :class:`JsonRequestError` for malformed, undecodable, or over-nested
    input so that callers have a single exception type to handle.
    """
    if isinstance(data, bytes | bytearray):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JsonRequestError("Request is not valid UTF-8") from exc
    else:
        text = data

    if scan_depth(text) > max_depth:
        raise JsonRequestError(
            f"Request nesting exceeds the maximum depth of {max_depth}"
        )

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonRequestError(str(exc)) from exc
    except RecursionError as exc:  # pragma: no cover - defence in depth
        raise JsonRequestError("Request is too deeply nested") from exc

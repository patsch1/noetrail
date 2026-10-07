"""Shared legacy and discovery-era MCP protocol helpers.

Noetrail keeps the 2024 initialize handshake for deployed clients while also
supporting the 2026 discovery handshake. The application servers own their
tool surfaces; this module owns only version negotiation and common result
envelopes so the knowledge server and the isolated fetcher cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

LEGACY_PROTOCOL_VERSION = "2024-11-05"
MODERN_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = (
    MODERN_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
)
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"


@dataclass(frozen=True)
class ProtocolRequestError(Exception):
    code: int
    message: str
    data: dict[str, object] | None = None


def request_protocol(request: dict[str, object]) -> str:
    """Return the request's protocol version, validating modern metadata.

    Legacy requests have no per-request protocol metadata. Modern requests
    carry both their negotiated version and client capabilities in ``_meta``.
    """

    params = request.get("params")
    if not isinstance(params, dict) or "_meta" not in params:
        return LEGACY_PROTOCOL_VERSION
    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        raise ProtocolRequestError(-32602, "params._meta must be an object")
    version = metadata.get(PROTOCOL_VERSION_META_KEY)
    capabilities = metadata.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(version, str):
        raise ProtocolRequestError(
            -32602,
            f"params._meta.{PROTOCOL_VERSION_META_KEY} must be a string",
        )
    if not isinstance(capabilities, dict):
        raise ProtocolRequestError(
            -32602,
            f"params._meta.{CLIENT_CAPABILITIES_META_KEY} must be an object",
        )
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolRequestError(
            -32022,
            "Unsupported protocol version",
            {
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "requested": version,
            },
        )
    return version


def result_envelope(
    result: dict[str, object],
    *,
    protocol: str,
    cacheable: bool = False,
) -> dict[str, object]:
    """Add fields required by discovery-era MCP without changing legacy."""

    if protocol != MODERN_PROTOCOL_VERSION:
        return result
    modern = dict(result)
    modern["resultType"] = "complete"
    if cacheable:
        modern["cacheScope"] = "private"
        modern["ttlMs"] = 0
    return modern


def discovery_result(
    *,
    name: str,
    version: str,
    instructions: str,
) -> dict[str, object]:
    return {
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": name, "version": version},
        "instructions": instructions,
        "cacheScope": "private",
        "ttlMs": 0,
        "resultType": "complete",
    }

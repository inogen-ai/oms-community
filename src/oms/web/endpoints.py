"""The public addresses a published bundle is told to call back on.

Kept dependency-free (imports nothing) so the settings, the HTTP factory and
the publisher can all derive the advertised URLs from the same constants as the
routes actually mounted. A published bundle is the one artefact nobody can
correct in place: it is copied onto other machines and read by agents there, so
an address that disagrees with the mounted route fails silently on somebody
else's laptop, long after the publish that baked it in.
"""
from __future__ import annotations

INGEST_ROUTE = "/api/ingest"

MCP_ROUTE = "/mcp"


def contribution_endpoint_for(public_url: str | None) -> str | None:
    """The POST address agents are told to send corrections to, or None when
    contribution is off."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}{INGEST_ROUTE}"


def mcp_endpoint_for(public_url: str | None) -> str | None:
    """The MCP address baked into `.mcp.json` and both installers, or None when
    contribution is off, so no config file can name a route nobody serves."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}{MCP_ROUTE}"

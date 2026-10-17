"""Authenticated, origin-checked MCP Streamable HTTP (2025-11-25 profile).

One POST maps to one MCP JSON-RPC message. Responses are application/json;
GET SSE and DELETE sessions return 405. No MCP-Session-Id is issued, so the
server uses the stateless option permitted by the 2025 Streamable HTTP spec.
TLS is supplied by a trusted reverse proxy; the socket should bind loopback.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from typing import Any

from .http import HttpRequest, HttpResponse, HttpServer
from .jsonrpc import JsonRpcError, Request, Response, parse_message
from .router import Router, RpcContext

PROFILE = "2025-11-25"
SUPPORTED = {"2025-11-25", "2025-06-18"}
ORIGIN_ERROR = "Origin is not approved for this MCP endpoint"


def _allowed_origin(value: str, permitted: Iterable[str]) -> bool:
    if not value:
        return True  # Non-browser clients typically omit Origin.
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return origin in set(permitted)
    except ValueError:
        return False


def register_mcp_http(
    server: HttpServer,
    mcp_router: Router,
    *,
    allowed_origins: Iterable[str] = (),
) -> None:
    """Bind one MCP route to an existing HttpServer with its bearer auth.

    The caller must configure the HTTP server with a named credential or token.
    Routes also require authenticated identities even for localhost.
    """
    allow = tuple(allowed_origins)
    server._mcp_allowed_origins = allow

    async def _handle(request: HttpRequest, identity: Any) -> HttpResponse:
        if identity is None:
            return HttpResponse(401, b"", headers={
                "WWW-Authenticate": 'Bearer realm="hcp-mcp"',
            })
        if not _allowed_origin(request.header("origin"), allow):
            return HttpResponse.error(403, ORIGIN_ERROR)
        if request.method != "POST":
            return HttpResponse(405, b"", headers={"Allow": "POST"})

        if request.header("content-type").split(";", 1)[0].strip().lower() != "application/json":
            return HttpResponse.error(415, "Content-Type must be application/json")
        accept = request.header("accept").lower()
        if "application/json" not in accept or "text/event-stream" not in accept:
            return HttpResponse.error(406, "Accept must include application/json and text/event-stream")

        # The 2026 stateless dialect requires MCP-Method and new method shapes.
        # Refuse it until fully implemented rather than mis-advertising compliance.
        requested = request.header("mcp-protocol-version")
        if requested and requested not in SUPPORTED:
            return HttpResponse.error(400, "unsupported MCP protocol version")
        if request.header("mcp-session-id"):
            return HttpResponse.error(400, "this endpoint does not issue MCP sessions")

        try:
            requests, batched = parse_message(request.body)
        except JsonRpcError as exc:
            return HttpResponse.json(Response.fail(None, exc).to_dict(), 400)
        if batched or len(requests) != 1:
            return HttpResponse.error(400, "MCP requires one JSON-RPC message per POST")
        req = requests[0]
        if req.method == "initialize":
            if not isinstance(req.params, dict):
                return HttpResponse.error(400, "initialize needs an object")
            client_version = req.params.get("protocolVersion", PROFILE)
            if client_version not in SUPPORTED:
                # Negotiate down from unrecognized versions to our actual profile.
                req = Request(req.method, {**req.params, "protocolVersion": PROFILE},
                              req.id, is_notification=req.is_notification)
        context = RpcContext(actor=identity.actor, transport="mcp-http", peer=request.peer)
        response = await mcp_router.dispatch(req, context)
        if response is None:
            return HttpResponse(202, b"")
        return HttpResponse.json(response.to_dict())

    for method in ("POST", "GET", "DELETE"):
        server.routes[(method, "/mcp")] = _handle

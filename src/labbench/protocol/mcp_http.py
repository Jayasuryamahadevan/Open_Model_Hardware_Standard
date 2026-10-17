"""Authenticated dual-era MCP Streamable HTTP for core JSON-result methods.

2026-07-28 uses independent per-request metadata and mandatory method/name
headers. 2025 clients retain their initialize path. Only the JSON response
subset is implemented: extensions, streaming subscriptions and MRTR are not
advertised. The device control safety kernel is shared across transports.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from typing import Any

from .http import HttpRequest, HttpResponse, HttpServer
from .jsonrpc import METHOD_NOT_FOUND, JsonRpcError, Request, Response, parse_message
from .mcp import LEGACY_VERSIONS, MODERN_VERSION, SUPPORTED_VERSIONS
from .router import Router, RpcContext

PROFILE = LEGACY_VERSIONS[0]
SUPPORTED = {MODERN_VERSION, *LEGACY_VERSIONS[:2]}
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

        requested = request.header("mcp-protocol-version")
        if requested and requested not in SUPPORTED:
            return HttpResponse.json(Response.fail(None, JsonRpcError(
                -32022, "Unsupported protocol version",
                {"supported": list(SUPPORTED_VERSIONS), "requested": requested},
            )).to_dict(), 400)
        if request.header("mcp-session-id"):
            return HttpResponse.error(400, "this endpoint does not issue MCP sessions")
        try:
            requests, batched = parse_message(request.body)
        except JsonRpcError as exc:
            return HttpResponse.json(Response.fail(None, exc).to_dict(), 400)
        if batched or len(requests) != 1:
            return HttpResponse.error(400, "MCP requires one JSON-RPC message per POST")
        req = requests[0]
        params = req.params if isinstance(req.params, dict) else {}
        meta = params.get("_meta", {})
        declared = (
            meta.get("io.modelcontextprotocol/protocolVersion")
            if isinstance(meta, dict) else None
        )
        if declared and declared not in SUPPORTED:
            return HttpResponse.json(Response.fail(req.id, JsonRpcError(
                -32022, "Unsupported protocol version",
                {"supported": list(SUPPORTED_VERSIONS), "requested": declared},
            )).to_dict(), 400)
        modern = requested == MODERN_VERSION or declared == MODERN_VERSION
        if modern:
            expected_name = (
                params.get("name") if req.method in ("tools/call", "prompts/get")
                else params.get("uri") if req.method == "resources/read" else None
            )
            header_name = request.header("mcp-name")
            if (requested != MODERN_VERSION or declared != MODERN_VERSION
                    or request.header("mcp-method") != req.method
                    or (expected_name is not None and header_name != expected_name)
                    or (expected_name is None and header_name)
                    or (expected_name is not None and not isinstance(expected_name, str))):
                return HttpResponse.json(Response.fail(req.id, JsonRpcError(
                    -32020, "MCP header/body mismatch",
                )).to_dict(), 400)
            if not isinstance(meta, dict) or not isinstance(
                meta.get("io.modelcontextprotocol/clientCapabilities"), dict
            ):
                return HttpResponse.json(Response.fail(req.id, JsonRpcError(
                    -32602, "MCP modern request requires clientCapabilities",
                )).to_dict(), 400)
            if not mcp_router.has(req.method):
                return HttpResponse.json(Response.fail(req.id, JsonRpcError(
                    METHOD_NOT_FOUND, f"Unknown MCP method {req.method!r}",
                )).to_dict(), 404)
        elif requested and requested not in SUPPORTED:
            return HttpResponse.error(400, "unsupported MCP version")
        if req.method == "initialize":
            if not isinstance(req.params, dict):
                return HttpResponse.error(400, "initialize needs an object")
            client_version = req.params.get("protocolVersion", PROFILE)
            if client_version not in LEGACY_VERSIONS:
                req = Request(req.method, {**req.params, "protocolVersion": PROFILE},
                              req.id, is_notification=req.is_notification)
        context = RpcContext(actor=identity.actor, transport="mcp-http", peer=request.peer)
        response = await mcp_router.dispatch(req, context)
        if response is None:
            return HttpResponse(202, b"")
        return HttpResponse.json(response.to_dict())

    for method in ("POST", "GET", "DELETE"):
        server.routes[(method, "/mcp")] = _handle

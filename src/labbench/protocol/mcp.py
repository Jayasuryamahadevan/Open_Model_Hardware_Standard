"""Local MCP stdio adapter for the existing LabBench gateway.

MCP is the northbound protocol. All tool calls still pass through the gateway
router and its safety kernel; this module has no direct device access.
"""

from __future__ import annotations

import json
from typing import Any

from .. import __version__
from ..bridge.schema import ToolSpec
from ..bridge.toolset import tool_specs
from ..core.analysis import mcp_control_name
from .jsonrpc import INVALID_PARAMS, JsonRpcError, Request
from .router import Router, RpcContext
from .stdio import StdioServer

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)
CACHEABLE_METHODS = {"server/discover", "tools/list", "resources/list", "resources/read",
                     "prompts/list", "prompts/get"}
CORE_TOOLS = frozenset({
    "hardware.analyze", "hardware.manual", "hardware.activate",
    "lab.describe", "lab.find", "lab.snapshot",
    "device.describe", "device.manuals", "device.manual", "device.read",
    "device.simulate", "device.invoke", "device.initialize",
    "job.status", "job.wait", "job.cancel", "estop",
})


class McpRouter(Router):
    """Translate MCP 2026 stateless envelopes without changing the device gateway."""

    async def dispatch(self, request: Request, ctx: RpcContext) -> Any:
        from .jsonrpc import Response

        params = request.params
        meta = params.get("_meta", {}) if isinstance(params, dict) else {}
        declared = (meta.get("io.modelcontextprotocol/protocolVersion")
                    if isinstance(meta, dict) else None)
        if declared is not None and declared not in SUPPORTED_VERSIONS:
            return Response.fail(request.id, JsonRpcError(
                -32022, "Unsupported protocol version",
                {"supported": list(SUPPORTED_VERSIONS), "requested": declared},
            ))
        modern = declared == MODERN_VERSION
        if modern:
            if not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
                return Response.fail(request.id, JsonRpcError(
                    INVALID_PARAMS, "modern MCP requires per-request clientCapabilities"
                ))
            if request.method in ("initialize", "notifications/initialized", "ping"):
                return Response.fail(request.id, JsonRpcError(
                    INVALID_PARAMS, "legacy lifecycle methods are unavailable in modern MCP"
                ))
            request = Request(
                request.method,
                {key: value for key, value in params.items() if key != "_meta"},
                request.id, is_notification=request.is_notification,
            )
        response = await super().dispatch(request, ctx)
        if modern and response is not None and response.kind == "result":
            result = response.result
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("resultType", "complete")
                result.setdefault("_meta", {}).setdefault(
                    "io.modelcontextprotocol/serverInfo",
                    {"name": "labbench-hcp", "version": __version__},
                )
                if request.method in CACHEABLE_METHODS:
                    # Device status and permitted tools can change at any time.
                    result.update({"ttlMs": 0, "cacheScope": "private"})
            response = Response.ok(response.id, result)
        return response



def _device_tools(gateway: Any) -> dict[str, tuple[ToolSpec, str, str, str]]:
    """Project verified live capabilities into typed, device-specific MCP tools."""
    out: dict[str, tuple[ToolSpec, str, str, str]] = {}
    for device_id, device in gateway.devices.all().items():
        if device_id in gateway.devices.quarantined or not device.state.operational:
            continue
        for feature in device.features().values():
            for command in feature.commands:
                if "raw" in command.tags:
                    continue  # the escape hatch is not advertised as a safe device control
                identity = f"{device_id}.{feature.identifier}.{command.name}"
                name = mcp_control_name(device_id, feature.identifier, command.name)
                schema = {"type": "object", "properties": {
                    "args": command.input_schema(),
                    "reason": {"type": "string", "description": "Reason for this action."},
                    "approval_id": {"type": "string", "description": "Granted approval, if required."},
                }, "required": ["args"], "additionalProperties": False}
                spec = ToolSpec(
                    name=name,
                    description=(f"{device.descriptor.display_name}: {identity}. "
                                 f"{command.description} Parameters are inside args. "
                                 "Execution passes through HCP's safety kernel."),
                    parameters=schema, hazard=command.hazard.value,
                    destructive=command.reversibility.value == "irreversible",
                    tags=["device-command"],
                )
                out[name] = (spec, device_id, feature.identifier, command.name)
    return out


def mcp_router(gateway: Any, *, profile: str = "core") -> Router:
    """Expose a concise default tool catalog; opt in to full workflow tooling."""
    if profile not in ("core", "full"):
        raise ValueError("MCP profile must be core or full")
    router = McpRouter()
    all_specs = tool_specs(gateway)
    specs = all_specs if profile == "full" else [
        spec for spec in all_specs if spec.name in CORE_TOOLS
    ]
    by_name = {spec.wire_name(): spec for spec in specs}
    if len(by_name) != len(specs):
        raise ValueError("MCP tool names collide after sanitisation")

    @router.method("initialize")
    async def initialize(protocolVersion: str = "2025-11-25", **_ignored: Any) -> dict[str, Any]:
        version = protocolVersion if protocolVersion in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True},
                             "prompts": {"listChanged": False}},
            "serverInfo": {"name": "labbench-hcp", "version": __version__},
            "instructions": (
                "Start with hardware_analyze, then lab_describe. For a matched reviewed "
                "binding, read its manual then call hardware_activate; the tool list will "
                "gain typed controls for that device. "
                "Use device_simulate before physical actions. Unknown hardware is only an "
                "inventory item until a reviewed driver/profile is configured. "
                "Every physical action must use device_invoke and obey its approval result."
            ),
        }

    @router.method("server/discover")
    async def server_discover() -> dict[str, Any]:
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "instructions": (
                "Call hardware_analyze to inspect connected USB and network interfaces. "
                "Only verified, reviewed adapters expose physical controls; "
                "use device_simulate and honor safety approvals before acting."
            ),
            "ttlMs": 0,
            "cacheScope": "private",
        }

    @router.method("notifications/initialized")
    async def initialized() -> None:
        return None

    @router.method("ping")
    async def ping() -> dict[str, Any]:
        return {}

    @router.method("tools/list")
    async def tools_list(cursor: str | None = None) -> dict[str, Any]:
        if cursor is not None:
            raise JsonRpcError(INVALID_PARAMS, "this tool list has no cursor")
        all_specs = [*specs, *(entry[0] for entry in _device_tools(gateway).values())]
        return {"tools": [
            {
                "name": spec.wire_name(),
                "description": spec.full_description(),
                "inputSchema": spec.parameters,
                "annotations": {"readOnlyHint": spec.read_only,
                                "destructiveHint": spec.destructive,
                                "idempotentHint": spec.idempotent},
            }
            for spec in sorted(all_specs, key=lambda item: item.wire_name())
        ]}

    @router.method("tools/call")
    async def tools_call(name: str, arguments: dict[str, Any] | None = None,
                         ctx: RpcContext | None = None) -> dict[str, Any]:
        if profile == "core" and name == "hardware_discover":
            name = "hardware_analyze"
        dynamic = _device_tools(gateway).get(name)
        spec = by_name.get(name)
        if spec is None and dynamic is None:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool {name!r}")
        if arguments is not None and not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "tool arguments must be an object")
        if dynamic is not None:
            _, device_id, feature, command = dynamic
            allowed = {"args", "reason", "approval_id"}
            extra = set(arguments or {}) - allowed
            if extra or not isinstance((arguments or {}).get("args"), dict):
                raise JsonRpcError(INVALID_PARAMS, "device tool needs an args object and no extra keys")
            method = "device.invoke"
            params = {"device": device_id, "feature": feature, "command": command,
                      "args": arguments["args"], "reason": arguments.get("reason", ""),
                      "approval_id": arguments.get("approval_id")}
        else:
            method, params = spec.name, arguments or {}
        inner = await gateway.router.dispatch(
            Request(method, params, id=1),
            RpcContext(actor=ctx.actor if ctx else "agent:mcp", transport="mcp-stdio"),
        )
        assert inner is not None
        payload = inner.to_dict()
        if "error" in payload:
            return {"content": [{"type": "text", "text": json.dumps(payload["error"])}],
                    "isError": True}
        result = payload.get("result")
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "structuredContent": result if isinstance(result, dict) else {"result": result}}

    @router.method("resources/list")
    async def resources_list(cursor: str | None = None) -> dict[str, Any]:
        if cursor is not None:
            raise JsonRpcError(INVALID_PARAMS, "this resource list has no cursor")
        return {"resources": [
            {"uri": f"hcp://device/{device_id}", "name": device.descriptor.display_name,
             "description": "Identity, state and declared capabilities", "mimeType": "application/json"}
            for device_id, device in sorted(gateway.devices.all().items())
        ]}

    @router.method("resources/read")
    async def resources_read(uri: str) -> dict[str, Any]:
        prefix = "hcp://device/"
        if not uri.startswith(prefix) or not uri[len(prefix):]:
            raise JsonRpcError(INVALID_PARAMS, f"unknown resource {uri!r}")
        inner = await gateway.router.dispatch(
            Request("device.describe", {"device": uri[len(prefix):]}, id=1),
            RpcContext(actor="agent:mcp", transport="mcp-stdio"),
        )
        assert inner is not None
        payload = inner.to_dict()
        if "error" in payload:
            raise JsonRpcError(INVALID_PARAMS, payload["error"]["message"])
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(payload["result"], default=str)}]}

    @router.method("prompts/list")
    async def prompts_list(cursor: str | None = None) -> dict[str, Any]:
        return {"prompts": [{"name": "inspect-hardware", "description":
                             "Identify connected hardware and read its capabilities before control."}]}

    @router.method("prompts/get")
    async def prompts_get(name: str) -> dict[str, Any]:
        if name != "inspect-hardware":
            raise JsonRpcError(INVALID_PARAMS, f"unknown prompt {name!r}")
        return {"messages": [{"role": "user", "content": {"type": "text", "text":
            "Call hardware_analyze first. Report observed endpoints and their evidence, then read "
            "device descriptions and manuals. Do not infer commands from a model name. "
            "For a requested physical action, simulate it and call the declared command "
            "through the gateway; stop if the driver or safety policy cannot authorize it."}}]}

    return router


async def serve_mcp_stdio(gateway: Any, *, profile: str = "core") -> None:
    server = StdioServer(mcp_router(gateway, profile=profile), actor="agent:mcp")

    async def on_gateway_event(topic: str, payload: dict[str, Any]) -> None:
        if topic == "tools.list_changed":
            await server._notify("notifications/tools/list_changed", {})
            await server._notify("notifications/resources/list_changed", {})

    gateway.add_event_sink(on_gateway_event)
    await server.serve()

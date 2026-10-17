"""Local MCP stdio adapter for the existing LabBench gateway.

MCP is the northbound protocol. All tool calls still pass through the gateway
router and its safety kernel; this module has no direct device access.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import __version__
from ..bridge.schema import ToolSpec, sanitise_name
from ..bridge.toolset import tool_specs
from .jsonrpc import INVALID_PARAMS, JsonRpcError, Request
from .router import Router, RpcContext
from .stdio import StdioServer

SUPPORTED_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2024-11-05")


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
                suffix = hashlib.sha256(identity.encode()).hexdigest()[:8]
                name = f"hcp_{sanitise_name(identity)[:50]}_{suffix}"
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


def mcp_router(gateway: Any) -> Router:
    """Build an MCP endpoint with stable tool names and live device resources."""
    router = Router()
    specs = tool_specs(gateway)
    by_name = {spec.wire_name(): spec for spec in specs}
    if len(by_name) != len(specs):
        raise ValueError("MCP tool names collide after sanitisation")

    @router.method("initialize")
    async def initialize(protocolVersion: str = "2025-11-25", **_ignored: Any) -> dict[str, Any]:
        version = protocolVersion if protocolVersion in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True},
                             "prompts": {"listChanged": False}},
            "serverInfo": {"name": "labbench-hcp", "version": __version__},
            "instructions": (
                "Start with lab_describe and hardware_discover. For a matched reviewed "
                "binding, read its manual then call hardware_activate; the tool list will "
                "gain typed controls for that device. "
                "Use device_simulate before physical actions. Unknown hardware is only an "
                "inventory item until a reviewed driver/profile is configured. "
                "Every physical action must use device_invoke and obey its approval result."
            ),
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
            for spec in all_specs
        ]}

    @router.method("tools/call")
    async def tools_call(name: str, arguments: dict[str, Any] | None = None,
                         ctx: RpcContext | None = None) -> dict[str, Any]:
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
            for device_id, device in gateway.devices.all().items()
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
            "Discover the hardware, report identity evidence and confidence, then read "
            "device descriptions and manuals. Do not infer commands from a model name. "
            "For a requested physical action, simulate it and call the declared command "
            "through the gateway; stop if the driver or safety policy cannot authorize it."}}]}

    return router


async def serve_mcp_stdio(gateway: Any) -> None:
    server = StdioServer(mcp_router(gateway), actor="agent:mcp")

    async def on_gateway_event(topic: str, payload: dict[str, Any]) -> None:
        if topic == "tools.list_changed":
            await server._notify("notifications/tools/list_changed", {})
            await server._notify("notifications/resources/list_changed", {})

    gateway.add_event_sink(on_gateway_event)
    await server.serve()

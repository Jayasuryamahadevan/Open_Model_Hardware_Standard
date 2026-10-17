"""Hermetic simulated HCP/MCP smoke harness; never enumerate host hardware."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from .core.registry import DeviceConfig, LabConfig
from .gateway import Gateway
from .protocol.jsonrpc import Request
from .protocol.mcp import MODERN_VERSION, mcp_router
from .protocol.router import RpcContext


async def run_harness(config: LabConfig | None = None) -> dict[str, Any]:
    """Exercise both MCP eras and tool projections without operating hardware.

    Refuse real-device configs so invoking a development harness cannot
    accidentally activate physical hardware.
    """
    configuration = config or LabConfig(
        name="hcp-smoke", devices=[DeviceConfig(id="arm", driver="sim_robot_arm")]
    )
    if (not configuration.devices
            or any(not item.driver.startswith("sim_") for item in configuration.devices)
            or configuration.scpi_targets or configuration.scpi_bindings
            or any(item.probe for item in configuration.network_targets)):
        raise ValueError("smoke harness accepts simulated devices only")
    tests: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hcp-harness-") as directory:
        gateway = Gateway(configuration, data_dir=Path(directory))
        await gateway.start()
        try:
            router = mcp_router(gateway)
            ctx = RpcContext(actor="agent:harness", transport="mcp-test")

            async def request(method: str, params: dict[str, Any] | None = None,
                              modern: bool = True) -> dict[str, Any]:
                kwargs = dict(params or {})
                if modern:
                    kwargs["_meta"] = {
                        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "hcp-harness", "version": "1"
                        },
                    }
                reply = await router.dispatch(Request(method, kwargs, id=1), ctx)
                assert reply is not None
                payload = reply.to_dict()
                if "error" in payload:
                    raise AssertionError(f"{method}: {payload['error']}")
                return payload["result"]

            async def check(name: str, fn: Any) -> None:
                try:
                    await fn()
                    tests.append({"name": name, "passed": True})
                except Exception as exc:  # noqa: BLE001 - harness reports all failed checks
                    tests.append({"name": name, "passed": False,
                                  "detail": f"{type(exc).__name__}: {exc}"})

            async def modern_discover() -> None:
                result = await request("server/discover")
                assert result["resultType"] == "complete"
                assert MODERN_VERSION in result["supportedVersions"]
                assert result["capabilities"]["tools"] is not None

            async def modern_tools() -> None:
                result = await request("tools/list")
                names = [item["name"] for item in result["tools"]]
                assert names == sorted(names)
                assert "hardware_analyze" in names
                assert result["ttlMs"] == 0
                assert "cacheScope" in result

            async def analyze() -> None:
                result = await request("tools/call", {
                    "name": "hardware_analyze", "arguments": {},
                })
                assert not result.get("isError")
                assert result["structuredContent"]["analysis"]["policy"]["auto_activation"] is False

            async def capability() -> None:
                device_id = configuration.devices[0].id
                result = await request("tools/call", {
                    "name": "device_describe", "arguments": {"device": device_id},
                })
                assert not result.get("isError")
                assert result["structuredContent"]["descriptor"]["simulated"] is True

            async def legacy() -> None:
                result = await request("initialize", {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "hcp-harness", "version": "1"},
                }, modern=False)
                assert result["protocolVersion"] == "2025-11-25"

            await check("modern discovery", modern_discover)
            await check("deterministic MCP schema catalog", modern_tools)
            with patch(
                "labbench.core.discovery.discover_hardware",
                new_callable=AsyncMock,
            ) as inventory:
                inventory.return_value = {
                    "usb": [], "native": [], "serial": [], "visa": [], "scpi": [],
                }
                await check("read-only hardware analysis", analyze)
            await check("verified simulated capabilities", capability)
            await check("legacy client handshake", legacy)
        finally:
            await gateway.close()
    return {
        "passed": all(item["passed"] for item in tests),
        "checks": tests,
        "mode": "simulated-only",
        "real_hardware_certified": False,
    }

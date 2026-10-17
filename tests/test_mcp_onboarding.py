"""MCP wire surface and evidence-only laptop discovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from labbench.core.discovery import scpi_identity, usb_inventory
from labbench.core.registry import DeviceConfig, LabConfig
from labbench.gateway import Gateway
from labbench.protocol.jsonrpc import Request
from labbench.protocol.mcp import mcp_router
from labbench.protocol.router import RpcContext


def test_usb_inventory_reports_identity_without_opening_device(tmp_path: Path):
    node = tmp_path / "sys" / "1-2"
    node.mkdir(parents=True)
    for name, value in {"idVendor": "1234", "idProduct": "ABCD", "product": "Arm",
                        "manufacturer": "Example", "serial": "SN-7"}.items():
        (node / name).write_text(value)
    found = usb_inventory(tmp_path / "sys", tmp_path / "no-serial")
    assert found[0]["vendor_id"] == "1234"
    assert found[0]["product_id"] == "abcd"
    assert found[0]["serial"] == "SN-7"
    assert found[0]["control_status"] == "unconfigured"


async def test_scpi_probe_does_not_send_motion_or_configure_commands(monkeypatch):
    sent = []

    class FakeTransport:
        def __init__(self, host, port, timeout_s):
            self.host, self.port = host, port

        async def open(self):
            pass

        async def query(self, command):
            sent.append(command)
            return "Acme,AX-1,SN42,1.2"

        async def close(self):
            pass

    monkeypatch.setattr("labbench.drivers.scpi.ScpiTransport", FakeTransport)
    found = await scpi_identity("192.0.2.8:5025")
    assert sent == ["*IDN?"]
    assert found["model"] == "AX-1"
    assert found["control_status"] == "unconfigured"


async def test_mcp_maps_calls_to_gateway_and_exposes_manual(tmp_path: Path):
    manual = tmp_path / "arm-manual.txt"
    manual.write_text("Home the arm before moving it. Keep clear of the workspace.")
    cfg = LabConfig(devices=[DeviceConfig(id="scope", driver="sim_microscope",
                                          manuals=[str(manual)])])
    gw = Gateway(cfg, data_dir=tmp_path / "data")
    await gw.start()
    try:
        router = mcp_router(gw)
        ctx = RpcContext(actor="agent:mcp", transport="mcp-stdio")

        async def call(method, params=None):
            response = await router.dispatch(Request(method, params or {}, id=1), ctx)
            assert response is not None
            return response.to_dict()["result"]

        init = await call("initialize", {"protocolVersion": "2025-11-25"})
        assert init["capabilities"]["tools"]
        tools = await call("tools/list")
        names = {tool["name"] for tool in tools["tools"]}
        assert {"lab_describe", "hardware_discover", "device_invoke", "device_manual"} <= names
        described = await call("tools/call", {"name": "device_describe",
                                               "arguments": {"device": "scope"}})
        assert described["structuredContent"]["descriptor"]["simulated"] is True
        manual_result = await call("tools/call", {"name": "device_manual",
                                                   "arguments": {"device": "scope"}})
        assert "Home the arm" in manual_result["structuredContent"]["text"]
        assert len(manual_result["structuredContent"]["sha256"]) == 64
        resource = await call("resources/read", {"uri": "hcp://device/scope"})
        assert json.loads(resource["contents"][0]["text"])["device"] == "scope"
        error = await call("tools/call", {"name": "device_invoke",
                                          "arguments": {"device": "scope", "feature": "Fake",
                                                        "command": "move"}})
        assert error["isError"] is True
    finally:
        await gw.close()


def test_mcp_stdio_subprocess_handshake(tmp_path: Path):
    repo = Path(__file__).resolve().parent.parent
    commands = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "labbench.cli", "--data-dir", str(tmp_path), "serve",
         "-c", str(repo / "configs" / "simulated-lab.yaml"), "-t", "mcp"],
        input="\n".join(json.dumps(command) for command in commands) + "\n",
        text=True, capture_output=True, timeout=20, cwd=repo, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    replies = {reply["id"]: reply for reply in map(json.loads, proc.stdout.splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "labbench-hcp"
    assert any(tool["name"] == "hardware_discover" for tool in replies[2]["result"]["tools"])


async def test_arm_wave_flow_through_mcp(tmp_path: Path):
    config_path = Path(__file__).resolve().parent.parent / "configs" / "simulated-arm.yaml"
    gw = Gateway(LabConfig.load(config_path), data_dir=tmp_path / "data")
    await gw.start()
    try:
        router = mcp_router(gw)
        ctx = RpcContext(actor="agent:mcp", transport="mcp-stdio")

        async def tool(name: str, arguments: dict):
            response = await router.dispatch(Request("tools/call", {"name": name,
                                                        "arguments": arguments}, id=1), ctx)
            assert response is not None
            return response.to_dict()["result"]

        described = await tool("device_describe", {"device": "arm1"})
        assert described["structuredContent"]["descriptor"]["model"] == "WAVE-1"
        manual = await tool("device_manual", {"device": "arm1"})
        assert "home before" in manual["structuredContent"]["text"].lower()
        before = await tool("device_simulate", {"device": "arm1", "feature": "MotionControl",
                                                "command": "wave"})
        assert before["structuredContent"]["feasible"] is False
        homed = await tool("device_invoke", {"device": "arm1", "feature": "MotionControl",
                                             "command": "home", "reason": "prepare greeting"})
        assert homed["structuredContent"]["homed"] is True
        predicted = await tool("device_simulate", {"device": "arm1", "feature": "MotionControl",
                                                   "command": "wave"})
        assert predicted["structuredContent"]["feasible"] is True
        waved = await tool("device_invoke", {"device": "arm1", "feature": "MotionControl",
                                             "command": "wave", "reason": "say hi"})
        assert waved["structuredContent"]["gesture"] == "wave"
        state = await tool("device_read", {"device": "arm1", "feature": "MotionControl",
                                           "property": "last_gesture"})
        assert state["structuredContent"]["value"] == "wave"
    finally:
        await gw.close()


async def test_initialize_obeys_motion_approval(tmp_path: Path):
    cfg = LabConfig(devices=[DeviceConfig(id="arm", driver="sim_robot_arm")],
                    safety={"autonomy": 0})
    gw = Gateway(cfg, data_dir=tmp_path / "data")
    await gw.start()
    try:
        router = mcp_router(gw)
        response = await router.dispatch(
            Request("tools/call", {"name": "device_initialize",
                                   "arguments": {"device": "arm", "reason": "prepare"}}, id=1),
            RpcContext(actor="agent:mcp", transport="mcp-stdio"),
        )
        assert response is not None
        result = response.to_dict()["result"]
        assert result["isError"] is True
        assert "approval" in result["content"][0]["text"].lower()
        assert gw.device("arm").homed is False
    finally:
        await gw.close()


async def test_identity_mismatch_quarantines_controls_even_after_fault_clear(tmp_path: Path):
    cfg = LabConfig(devices=[DeviceConfig(id="arm", driver="sim_robot_arm", model="WAVE-1",
                                          expected_identity={"model": "WAVE-2"})])
    gw = Gateway(cfg, data_dir=tmp_path / "data")
    boot = await gw.start()
    try:
        assert "identity mismatch" in boot["connected"]["arm"]
        await gw.device("arm").clear_fault()
        router = mcp_router(gw)
        response = await router.dispatch(
            Request("tools/call", {"name": "device_invoke", "arguments":
                                   {"device": "arm", "feature": "MotionControl",
                                    "command": "home", "reason": "test"}}, id=1),
            RpcContext(actor="agent:mcp", transport="mcp-stdio"),
        )
        assert response is not None
        assert response.to_dict()["result"]["isError"] is True
        assert gw.device("arm").homed is False
    finally:
        await gw.close()


async def test_analyze_activate_publishes_typed_tools_for_verified_device(monkeypatch, tmp_path: Path):
    from labbench.core.registry import ScpiBinding

    class FakeTransport:
        async def open(self):
            pass

        async def query(self, command):
            if command == "*IDN?":
                return "Acme,DMM-1,SN123,FW1.0"
            return '0,"No error"'

        async def close(self):
            pass

        async def write(self, command):
            raise AssertionError(f"unexpected write during onboarding: {command}")

    monkeypatch.setattr("labbench.drivers.scpi.ScpiTransport", lambda *a, **kw: FakeTransport())
    cfg = LabConfig(scpi_bindings=[ScpiBinding(
        id="meter1", address="192.0.2.20:5025", vendor="Acme", model="DMM-1",
        serial="SN123", firmware="FW1.0", profile="dmm", kind="multimeter",
    )])
    gw = Gateway(cfg, data_dir=tmp_path / "data")
    await gw.start()
    try:
        router = mcp_router(gw)
        ctx = RpcContext(actor="agent:mcp", transport="mcp-stdio")

        async def call(method: str, params: dict):
            response = await router.dispatch(Request(method, params, id=1), ctx)
            assert response is not None
            return response.to_dict()["result"]

        analysis = await call("tools/call", {"name": "hardware_discover", "arguments": {}})
        binding = analysis["structuredContent"]["bindings"][0]
        assert binding["ready_to_activate"] is True
        activated = await call("tools/call", {"name": "hardware_activate",
                                               "arguments": {"rule_id": "meter1"}})
        assert activated["structuredContent"]["descriptor"]["serial"] == "SN123"
        listed = await call("tools/list", {})
        measure = next(tool for tool in listed["tools"]
                       if tool["name"].startswith("hcp_meter1_Measurement_measure"))
        assert "args" in measure["inputSchema"]["properties"]
        result = await call("tools/call", {"name": measure["name"],
                                            "arguments": {"args": {}, "reason": "read voltage"}})
        assert result["structuredContent"]["reply"] == '0,"No error"'
    finally:
        await gw.close()

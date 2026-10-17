"""Modern MCP 2026, USB serial and private Wi-Fi onboarding regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labbench.core.device import DeviceDescriptor, DeviceState
from labbench.core.errors import ValidationError
from labbench.core.network_discovery import NetworkTarget, inspect_network_targets
from labbench.core.registry import DeviceConfig, LabConfig
from labbench.drivers.serial_ascii import SerialAsciiDevice, _frame
from labbench.gateway import Gateway
from labbench.harness import run_harness
from labbench.protocol.auth import Credential
from labbench.protocol.http import HttpRequest, HttpServer
from labbench.protocol.jsonrpc import Request
from labbench.protocol.mcp import MODERN_VERSION, mcp_router
from labbench.protocol.mcp_http import register_mcp_http
from labbench.protocol.router import Router, RpcContext


META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "unit-test", "version": "1"},
}


def _serial_config() -> dict:
    return {
        "port": "/dev/ttyACM0",
        "usb_identity": {"vid": 0x1234, "pid": 0x5678, "serial_number": "ABC"},
        "identity_query": "ID?",
        "identity_reply": "ACME,CTRL,ABC",
        "features": [{
            "identifier": "Light",
            "properties": [{
                "name": "value", "schema": {"name": "value", "type": "string"},
                "query": "LEVEL?",
            }],
            "commands": [{
                "name": "set", "frame": "SET {value}",
                "hazard": "benign", "reversibility": "restorable",
                "parameters": [
                    {"name": "value", "type": "integer",
                     "constraint": {"minimum": 0, "maximum": 100}},
                ],
            }],
        }],
    }


async def test_serial_driver_pins_usb_identity_and_only_emits_allowlisted_frames(monkeypatch):
    serial = pytest.importorskip("serial")
    sent: list[bytes] = []

    class FakeSerial:
        def __init__(self, *args, **kwargs):
            self.responses = [b"ACME,CTRL,ABC\n", b"40\n"]

        def write(self, data):
            sent.append(data)

        def flush(self):
            pass

        def readline(self, size=4097):
            return self.responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr("labbench.drivers.serial_ascii._usb_identity", lambda port: {
        "vid": 0x1234, "pid": 0x5678, "serial_number": "ABC",
    })
    driver = SerialAsciiDevice(DeviceDescriptor(id="light"), **_serial_config())
    await driver.connect()
    try:
        assert driver.state is DeviceState.IDLE
        assert (await driver.read("Light", "value")).value == "40"
        assert (await driver.invoke("Light", "set", {"value": 25}))["sent"]
        with pytest.raises(ValidationError):
            await driver.invoke("Light", "set", {"value": 900})
        assert sent == [b"ID?\n", b"LEVEL?\n", b"SET 25\n"]
        assert driver.descriptor.serial == "ABC"
    finally:
        await driver.disconnect()


async def test_usb_mismatch_fails_before_opening_port(monkeypatch):
    serial = pytest.importorskip("serial")

    def unsafe(*args, **kwargs):
        raise AssertionError("must never open a mismatched USB port")

    monkeypatch.setattr(serial, "Serial", unsafe)
    monkeypatch.setattr("labbench.drivers.serial_ascii._usb_identity", lambda port: {
        "vid": 0x9999, "pid": 0x5678, "serial_number": "ABC",
    })
    driver = SerialAsciiDevice(DeviceDescriptor(id="light"), **_serial_config())
    with pytest.raises(ValidationError):
        await driver.connect()
    assert driver.state is DeviceState.FAULT


def test_serial_templates_reject_escape_sequences():
    cfg = _serial_config()
    cfg["features"][0]["commands"][0]["frame"] = "SET {value};REMOVE"
    with pytest.raises(ValidationError):
        SerialAsciiDevice(DeviceDescriptor(id="light"), **cfg)
    with pytest.raises(ValidationError):
        _frame("SET 3\nHOME")


def test_wifi_target_requires_local_address_and_serial_pin():
    with pytest.raises(ValueError):
        NetworkTarget(id="unsafe", url="https://public.example")
    with pytest.raises(ValueError):
        NetworkTarget(id="unsafe", url="http://user:pass@192.168.0.4")
    with pytest.raises(ValueError):
        NetworkTarget(id="unsafe", url="http://8.8.8.8")


async def test_wifi_probe_is_opt_in_and_identity_mismatch_keeps_unconfigured(monkeypatch):
    visited = []

    def fake_probe(target):
        visited.append(target.id)
        return {"vendor": "ACME", "model": "Thing", "serial": "SN-OTHER"}

    monkeypatch.setattr("labbench.core.network_discovery._probe", fake_probe)
    targets = [
        NetworkTarget(id="no-probe", url="http://192.168.1.12",
                      expected_identity={"serial": "SN-1"}),
        NetworkTarget(id="probe", url="http://192.168.1.15", probe=True,
                      expected_identity={"serial": "SN-1"}),
    ]
    results = await inspect_network_targets(targets)
    assert visited == ["probe"]
    assert results[0]["probe"] == "not_probed"
    assert results[1]["control_status"] == "unconfigured"
    assert "mismatch" in results[1]["reason"]


def _http_request(method: str, params: dict, *, headers=None) -> HttpRequest:
    hdrs = {
        "authorization": "Bearer operator-secret",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": MODERN_VERSION,
        "mcp-method": method,
    }
    if method in ("tools/call", "prompts/get"):
        hdrs["mcp-name"] = params["name"]
    if headers:
        hdrs.update(headers)
    body = {"jsonrpc": "2.0", "id": 7, "method": method,
            "params": {**params, "_meta": META}}
    return HttpRequest("POST", "/mcp", "HTTP/1.1", hdrs,
                       json.dumps(body).encode(), "127.0.0.1:3000")


async def test_modern_mcp_http_discovery_and_header_mismatch(tmp_path: Path):
    gw = Gateway(LabConfig(devices=[DeviceConfig(
        id="arm", driver="sim_robot_arm",
    )]), data_dir=tmp_path)
    await gw.start()
    try:
        server = HttpServer(Router(), credentials=[
            Credential(token="operator-secret", actor="agent:test"),
        ], allow_origin="")
        register_mcp_http(server, mcp_router(gw))
        discover = await server._handle(_http_request("server/discover", {}), None)
        assert discover.status == 200
        result = json.loads(discover.body)["result"]
        assert result["resultType"] == "complete"
        assert MODERN_VERSION in result["supportedVersions"]
        assert result["cacheScope"] == "private"
        tools = await server._handle(_http_request("tools/list", {}), None)
        assert tools.status == 200
        names = [t["name"] for t in json.loads(tools.body)["result"]["tools"]]
        assert names == sorted(names)
        wrong = await server._handle(_http_request(
            "tools/call", {"name": "hardware_analyze", "arguments": {}},
            headers={"mcp-name": "device_invoke"},
        ), None)
        assert wrong.status == 400
        assert json.loads(wrong.body)["error"]["code"] == -32020
        invalid = await server._handle(_http_request(
            "tools/list", {}, headers={"mcp-protocol-version": "1999-01-01"},
        ), None)
        assert invalid.status == 400
        assert json.loads(invalid.body)["error"]["code"] == -32022
        invoke = await server._handle(_http_request(
            "tools/call", {"name": "lab_describe", "arguments": {}},
        ), None)
        assert invoke.status == 200
        assert json.loads(invoke.body)["result"]["resultType"] == "complete"
    finally:
        await gw.close()


async def test_modern_mcp_stdio_router_protocol(tmp_path: Path):
    gw = Gateway(LabConfig(devices=[DeviceConfig(
        id="arm", driver="sim_robot_arm",
    )]), data_dir=tmp_path)
    await gw.start()
    try:
        router = mcp_router(gw)
        ctx = RpcContext(actor="agent:test")
        good = await router.dispatch(Request(
            "server/discover", {"_meta": META}, id=1,
        ), ctx)
        assert good.to_dict()["result"]["resultType"] == "complete"
        bad = await router.dispatch(Request(
            "tools/list", {"_meta": {
                **META, "io.modelcontextprotocol/protocolVersion": "1900-01-01",
            }}, id=2,
        ), ctx)
        assert bad.to_dict()["error"]["code"] == -32022
    finally:
        await gw.close()


async def test_simulation_harness_rejects_physical_and_probes():
    with pytest.raises(ValueError):
        await run_harness(LabConfig(devices=[
            DeviceConfig(id="meter", driver="scpi"),
        ]))
    with pytest.raises(ValueError):
        await run_harness(LabConfig(devices=[
            DeviceConfig(id="sim", driver="sim_robot_arm"),
        ], network_targets=[NetworkTarget(
            id="target", url="http://192.168.1.10", probe=True,
            expected_identity={"serial": "SN1"},
        )]))
    report = await run_harness()
    assert report["passed"] is True, report
    assert len(report["checks"]) >= 5
    assert report["real_hardware_certified"] is False

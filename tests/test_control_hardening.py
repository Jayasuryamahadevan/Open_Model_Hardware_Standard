"""Regression and failure-injection tests for hardware control hardening."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from labbench.core.analysis import build_analysis
from labbench.core.device import DeviceDescriptor, DeviceState
from labbench.core.discovery import native_inventory
from labbench.core.errors import DeviceNotReady, ValidationError
from labbench.core.registry import DeviceConfig, LabConfig
from labbench.drivers.http_api import HttpApiDevice
from labbench.gateway import Gateway
from labbench.protocol.http import HttpRequest, HttpServer
from labbench.protocol.mcp import mcp_router
from labbench.protocol.mcp_http import _allowed_origin, register_mcp_http
from labbench.protocol.router import Router


def test_windows_inventory_is_passive_and_sanitized(monkeypatch):
    commands = []

    def stub(command, timeout_s=8.0):
        commands.append(command)
        return [{"InstanceId": "USB\\VID_1234&PID_4567", "Class": "USB",
                 "FriendlyName": "Camera", "Manufacturer": "Acme", "Status": "OK"}]

    monkeypatch.setattr("labbench.core.discovery._native_query", stub)
    result = native_inventory("Windows")
    assert result[0]["control_status"] == "unconfigured"
    assert result[0]["model"] == "Camera"
    assert len(commands) == 1
    assert commands[0][0] == "powershell.exe"
    analysis = build_analysis({"native": result}, {}, {})
    assert analysis["summary"]["observed_endpoints"] == 1
    assert analysis["summary"]["controllable_devices"] == 0


def test_macos_inventory_nested_usb(monkeypatch):
    monkeypatch.setattr("labbench.core.discovery._native_query", lambda *a, **kw: {
        "SPUSBDataType": [{"_name": "Bus", "_items": [{
            "_name": "USB Arm", "vendor_id": "0x1234",
            "product_id": "0x5678", "serial_num": "ABC",
        }]}],
    })
    result = native_inventory("Darwin")
    assert len(result) == 1
    assert result[0]["serial"] == "ABC"
    assert result[0]["control_status"] == "unconfigured"


def _configuration(command_timeout_s=1.0, *, expected="SN1", estop=True):
    return {
        "base_url": "http://127.0.0.1:30000",
        "identity": {"serial": expected, "model": "API-1"},
        "identity_path": "/identity",
        "command_timeout_s": command_timeout_s,
        **({"estop_path": "/stop"} if estop else {}),
        "features": [{
            "identifier": "Light",
            "properties": [{
                "name": "level", "path": "/level",
                "schema": {"name": "level", "type": "integer"},
            }],
            "commands": [{
                "name": "set_level", "method": "POST", "path": "/level",
                "hazard": "benign", "reversibility": "restorable",
                "parameters": [{
                    "name": "level", "type": "integer",
                    "constraint": {"minimum": 0, "maximum": 100},
                }],
            }],
        }],
    }


def test_http_api_rejects_unsafe_routes():
    cfg = _configuration()
    cfg["features"][0]["commands"][0]["path"] = "//attacker.invalid/bad"
    with pytest.raises(ValidationError):
        HttpApiDevice(DeviceDescriptor(id="light"), **cfg)
    cfg = _configuration()
    cfg["features"][0]["commands"][0]["hazard"] = "none"
    with pytest.raises(ValidationError):
        HttpApiDevice(DeviceDescriptor(id="light"), **cfg)
    cfg = _configuration(estop=False)
    cfg["features"][0]["commands"][0]["hazard"] = "motion"
    with pytest.raises(ValidationError):
        HttpApiDevice(DeviceDescriptor(id="light"), **cfg)


async def test_http_api_identity_and_schema_through_mock_transport(monkeypatch):
    httpx = pytest.importorskip("httpx")
    observed = []
    original = httpx.AsyncClient

    def handler(request):
        observed.append((request.method, request.url.path))
        if request.url.path == "/identity":
            return httpx.Response(200, json={
                "vendor": "Acme", "model": "API-1", "serial": "SN1",
            })
        if request.url.path == "/level" and request.method == "GET":
            return httpx.Response(200, json={"value": 10})
        if request.url.path == "/level" and request.method == "POST":
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(404, json={"error": "unknown"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw
    ))
    driver = HttpApiDevice(DeviceDescriptor(id="light"), **_configuration())
    await driver.connect()
    try:
        assert driver.descriptor.serial == "SN1"
        assert (await driver.read("Light", "level")).value == 10
        assert (await driver.invoke("Light", "set_level", {"level": 50}))["accepted"]
        with pytest.raises(ValidationError):
            await driver.invoke("Light", "set_level", {"level": 101})
        assert observed.count(("POST", "/level")) == 1
        assert driver.state is DeviceState.IDLE
    finally:
        await driver.disconnect()


async def test_http_api_identity_mismatch_stays_faulted(monkeypatch):
    httpx = pytest.importorskip("httpx")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"serial": "WRONG", "model": "API-1"})
        ), **kw
    ))
    driver = HttpApiDevice(DeviceDescriptor(id="light"), **_configuration())
    with pytest.raises(ValidationError):
        await driver.connect()
    assert driver.state is DeviceState.FAULT
    with pytest.raises(DeviceNotReady):
        await driver.connect()


async def test_command_timeout_quarantines_device_state(monkeypatch):
    driver = HttpApiDevice(DeviceDescriptor(id="light"), **_configuration(
        command_timeout_s=0.1
    ))
    async def connect():
        pass

    async def stall(*args, **kwargs):
        await asyncio.sleep(0.4)

    monkeypatch.setattr(driver, "_connect", connect)
    monkeypatch.setattr(driver, "_invoke", stall)
    await driver.connect()
    with pytest.raises(DeviceNotReady):
        await driver.invoke("Light", "set_level", {"level": 20})
    assert driver.state is DeviceState.FAULT
    assert "timed out" in (driver.fault or "")
    with pytest.raises(DeviceNotReady):
        await driver.connect()


def _request(method, body=None, *, token="secret", origin=None, accept=True):
    headers = {"authorization": f"Bearer {token}",
               "content-type": "application/json"}
    if origin is not None:
        headers["origin"] = origin
    if accept:
        headers["accept"] = "application/json, text/event-stream"
    return HttpRequest(method, "/mcp", "HTTP/1.1", headers,
                       json.dumps(body).encode() if body is not None else b"",
                       "127.0.0.1:10001")


def test_origin_allowlist():
    assert _allowed_origin("", [])
    assert _allowed_origin("https://trusted.example", ["https://trusted.example"])
    assert not _allowed_origin("https://trusted.example.evil", ["https://trusted.example"])
    assert not _allowed_origin("https://alice:pwd@trusted.example", ["https://trusted.example"])
    assert not _allowed_origin("null", [])


async def test_streamable_http_auth_origin_and_tool_dispatch(tmp_path: Path):
    gw = Gateway(LabConfig(devices=[DeviceConfig(
        id="arm", driver="sim_robot_arm",
    )]), data_dir=tmp_path)
    await gw.start()
    try:
        server = HttpServer(Router(), token="secret", allow_origin="")
        register_mcp_http(server, mcp_router(gw),
                          allowed_origins=("https://trusted.example",))
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        }
        ok = await server._handle(_request("POST", body), None)
        assert ok is not None and ok.status == 200
        assert json.loads(ok.body)["result"]["protocolVersion"] == "2025-11-25"

        bad = await server._handle(
            _request("POST", body, origin="https://evil.example"), None,
        )
        assert bad is not None and bad.status == 403

        no_auth = await server._handle(_request("POST", body, token="bad"), None)
        assert no_auth is not None and no_auth.status == 401

        no_accept = await server._handle(_request("POST", body, accept=False), None)
        assert no_accept is not None and no_accept.status == 406

        get = await server._handle(_request("GET"), None)
        assert get is not None and get.status == 405

        listed = await server._handle(_request("POST", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }), None)
        assert listed is not None and listed.status == 200
        names = [tool["name"] for tool in json.loads(listed.body)["result"]["tools"]]
        assert "hardware_analyze" in names

        notice = await server._handle(_request("POST", {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }), None)
        assert notice is not None and notice.status == 202
    finally:
        await gw.close()

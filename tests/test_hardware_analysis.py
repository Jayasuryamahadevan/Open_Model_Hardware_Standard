"""Hardware endpoint analysis and MCP control contracts."""

from __future__ import annotations

from pathlib import Path

from labbench.cli import build_parser
from labbench.core.analysis import _configured_endpoint, build_analysis, mcp_control_name
from labbench.core.registry import DeviceConfig, LabConfig, ScpiBinding
from labbench.gateway import Gateway
from labbench.protocol.jsonrpc import Request
from labbench.protocol.mcp import mcp_router
from labbench.protocol.router import RpcContext


def test_analysis_exposes_observed_endpoints_not_unsafe_commands():
    inventory = {
        "usb": [{
            "bus_path": "1-2", "vendor_id": "1234", "product_id": "abcd",
            "vendor": "Vendor", "model": "Camera", "serial": "SN-5",
            "serial_ports": ["/dev/ttyACM0"],
        }],
        "serial": [{"port": "/dev/ttyACM0", "vendor_id": "1234"}],
        "visa": [{"address": "USB0::1234::abcd::SN5::INSTR"}, {"error": "backend missing"}],
        "scpi": [
            {"address": "192.0.2.15:5025", "vendor": "Acme", "model": "DMM",
             "serial": "7", "identity_reply": "Acme,DMM,7,1"},
            {"address": "192.0.2.16:5025", "error": "timeout"},
        ],
        "bindings": [
            {"rule_id": "meter", "address": "192.0.2.15:5025",
             "ready_to_activate": True},
        ],
    }
    result = build_analysis(inventory, {}, {})
    assert result["summary"] == {
        "observed_endpoints": 5, "unreviewed_endpoints": 4,
        "verified_bindings": 1, "configured_devices": 0,
        "controllable_devices": 0, "published_device_command_tools": 0,
    }
    usb, serial, visa, scpi, failed = result["endpoints"]
    assert usb["protocol"] == "usb-descriptor"
    assert serial["address"] == "/dev/ttyACM0"
    assert visa["protocol"] == "visa"
    assert scpi["control_status"] == "reviewed_binding_verified"
    assert scpi["reviewed_binding"] == "meter"
    assert failed["control_status"] == "probe_failed"
    assert all("commands" not in endpoint for endpoint in result["endpoints"])
    assert result["policy"]["auto_activation"] is False


def test_mcp_tool_names_are_stable_and_collision_resistant():
    assert mcp_control_name("meter1", "Measurement", "measure").startswith(
        "hcp_meter1_Measurement_measure_"
    )
    assert mcp_control_name("meter1", "Measurement", "measure") == mcp_control_name(
        "meter1", "Measurement", "measure"
    )
    assert mcp_control_name("meter1", "Measurement", "measure") != mcp_control_name(
        "meter2", "Measurement", "measure"
    )


async def test_analysis_publishes_only_configured_live_device_schemas(
    monkeypatch, tmp_path: Path
):
    async def discover(_targets):
        return {"usb": [], "serial": [], "visa": [], "scpi": []}

    monkeypatch.setattr("labbench.core.discovery.discover_hardware", discover)
    gw = Gateway(LabConfig(devices=[DeviceConfig(id="arm", driver="sim_robot_arm")]),
                 data_dir=tmp_path)
    await gw.start()
    try:
        router = mcp_router(gw)
        ctx = RpcContext(actor="agent:mcp", transport="mcp-stdio")
        response = await router.dispatch(Request("tools/call", {
            "name": "hardware_analyze", "arguments": {},
        }, id=1), ctx)
        assert response is not None
        result = response.to_dict()["result"]
        assert result.get("isError") is not True
        report = result["structuredContent"]["analysis"]
        assert report["summary"]["configured_devices"] == 1
        assert report["summary"]["controllable_devices"] == 1
        assert report["summary"]["published_device_command_tools"] > 0
        arm = report["devices"][0]
        assert arm["id"] == "arm"
        assert arm["controllable"] is True
        commands = [
            cmd for feature in arm["features"] for cmd in feature["commands"]
        ]
        wave = next(command for command in commands if command["name"] == "wave")
        assert wave["mcp_tool"].startswith("hcp_arm_MotionControl_wave_")
        assert wave["input_schema"]["type"] == "object"
        tools_response = await router.dispatch(
            Request("tools/list", {}, id=2), ctx
        )
        assert tools_response is not None
        tool_names = {tool["name"] for tool in
                      tools_response.to_dict()["result"]["tools"]}
        assert "hardware_analyze" in tool_names
        assert wave["mcp_tool"] in tool_names
    finally:
        await gw.close()


async def test_serialless_scpi_binding_is_not_activated(monkeypatch, tmp_path: Path):
    async def discover(_targets):
        return {
            "usb": [], "serial": [], "visa": [],
            "scpi": [{
                "address": "192.0.2.15:5025", "vendor": "Acme", "model": "DMM",
                "serial": "7", "identity_reply": "Acme,DMM,7,1",
            }],
        }

    monkeypatch.setattr("labbench.core.discovery.discover_hardware", discover)
    gw = Gateway(LabConfig(scpi_bindings=[ScpiBinding(
        id="meter", address="192.0.2.15:5025", vendor="Acme", model="DMM",
        profile="dmm",
    )]), data_dir=tmp_path)
    await gw.start()
    try:
        result = await gw.analyze_hardware()
        assert result["bindings"][0]["ready_to_activate"] is False
        assert "serial" in result["bindings"][0]["reason"]
        assert result["analysis"]["summary"]["verified_bindings"] == 0
        assert not gw.devices.all()
    finally:
        await gw.close()


def test_analyze_cli_has_optional_config():
    args = build_parser().parse_args(["analyze"])
    assert args.command == "analyze"
    assert args.config is None
    args = build_parser().parse_args(["analyze", "-c", "lab.yaml"])
    assert args.config == "lab.yaml"


def test_configured_endpoint_redacts_credentials_and_query():
    config = DeviceConfig(id="thing", driver="wot", settings={
        "td_url": "https://alice:secret@iot.example:443/td?token=private#ignored",
        "token": "do-not-return",
    })
    endpoint = _configured_endpoint(config)
    assert endpoint is not None
    assert endpoint["address"] == "https://iot.example:443/td"
    assert "secret" not in str(endpoint)
    assert "private" not in str(endpoint)


def test_bad_url_port_does_not_crash_hardware_report():
    config = DeviceConfig(id="thing", driver="wot", settings={
        "td_url": "https://iot.example:not-a-number/td"
    })
    assert _configured_endpoint(config) is None

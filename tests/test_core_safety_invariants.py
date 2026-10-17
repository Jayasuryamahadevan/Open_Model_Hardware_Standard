"""Safety invariants that must hold for every physical driver and MCP client."""

from __future__ import annotations

import asyncio

import pytest

from labbench.core.capability import Command, Feature, Hazard
from labbench.core.device import Device, DeviceDescriptor, DeviceState, ExecutionContext
from labbench.core.errors import DeviceNotReady, TransportError
from labbench.core.registry import DeviceConfig, DeviceManager, LabConfig
from labbench.drivers.simulated.microscope import SimulatedMicroscope
from labbench.gateway import Gateway
from labbench.protocol.jsonrpc import Request
from labbench.protocol.mcp import mcp_router
from labbench.protocol.router import RpcContext


class UnverifiedDevice(Device):
    def _features(self):
        return [Feature(
            identifier="Diagnostics",
            commands=[Command(name="query", hazard=Hazard.NONE)],
        )]

    async def _read(self, feature, name):
        return 0

    async def _invoke(self, feature, command, args, ctx):
        return {"ok": True}


async def test_unverified_configured_serial_cannot_self_certify():
    manager = DeviceManager()
    manager.registry.register("unverified-fixture", UnverifiedDevice)
    device = manager.add_from_config(DeviceConfig(
        id="fixture", driver="unverified-fixture", serial="EXPECTED",
        expected_identity={"serial": "EXPECTED"}, autoconnect=False,
    ))
    assert device.descriptor.serial is None
    status = await manager.connect_one("fixture", manual=True)
    assert status.startswith("error: identity mismatch")
    assert device.state is DeviceState.FAULT
    assert "fixture" in manager.quarantined
    assert (await manager.connect_one("fixture", manual=True)).startswith(
        "error: device quarantined"
    )


async def test_configured_model_is_not_observed_hardware_identity():
    manager = DeviceManager()
    manager.registry.register("unverified-fixture", UnverifiedDevice)
    device = manager.add_from_config(DeviceConfig(
        id="fixture", driver="unverified-fixture", vendor="ACME",
        model="FAKE-MODEL", expected_identity={"model": "FAKE-MODEL"},
        autoconnect=False,
    ))
    assert device.descriptor.vendor == "unknown"
    assert device.descriptor.model == "unknown"
    result = await manager.connect_one("fixture", manual=True)
    assert "identity mismatch" in result
    assert device.state is DeviceState.FAULT


async def test_cancelled_connect_never_lingers_in_connecting(monkeypatch):
    device = UnverifiedDevice(DeviceDescriptor(id="fixture"))

    async def slow_connect():
        await asyncio.sleep(30)

    monkeypatch.setattr(device, "_connect", slow_connect)
    task = asyncio.create_task(device.connect())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert device.state is DeviceState.FAULT


async def test_disconnect_failure_never_looks_offline(monkeypatch):
    device = UnverifiedDevice(DeviceDescriptor(id="fixture"))
    await device.connect()

    async def fail_disconnect():
        raise TransportError("USB cable vanished")

    monkeypatch.setattr(device, "_disconnect", fail_disconnect)
    with pytest.raises(TransportError):
        await device.disconnect()
    assert device.state is DeviceState.FAULT


async def test_fault_clearance_never_makes_device_operational():
    device = UnverifiedDevice(DeviceDescriptor(id="fixture"))
    await device.connect()
    await device.fail("uncertain physical state")
    await device.clear_fault()
    assert device.state is DeviceState.OFFLINE
    assert device.fault is None
    with pytest.raises(DeviceNotReady):
        await device.invoke("Diagnostics", "query", {})
    await device.connect()
    assert device.state is DeviceState.IDLE


async def test_read_only_command_cannot_bypass_offline_gate():
    device = UnverifiedDevice(DeviceDescriptor(id="fixture"))
    with pytest.raises(DeviceNotReady):
        await device.invoke("Diagnostics", "query", {})


async def test_queued_command_does_not_run_after_previous_fault(monkeypatch):
    device = UnverifiedDevice(DeviceDescriptor(id="fixture"))
    await device.connect()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fail_on_first(feature, command, args, ctx):
        calls.append(command)
        started.set()
        await release.wait()
        raise TransportError("device disconnected during operation")

    monkeypatch.setattr(device, "_invoke", fail_on_first)
    first = asyncio.create_task(device.invoke("Diagnostics", "query", {}))
    await started.wait()
    second = asyncio.create_task(device.invoke("Diagnostics", "query", {}))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(TransportError):
        await first
    with pytest.raises(DeviceNotReady):
        await second
    assert calls == ["query"]
    assert device.state is DeviceState.FAULT


async def test_human_connect_rpc_still_verifies_physical_identity(tmp_path):
    gateway = Gateway(LabConfig(devices=[DeviceConfig(
        id="fixture", driver="unverified-fixture", serial="FAKE",
        expected_identity={"serial": "FAKE"}, autoconnect=False,
    )]), data_dir=tmp_path)
    gateway.devices.registry.register("unverified-fixture", UnverifiedDevice)
    await gateway.start()
    try:
        async def call(method, actor, **params):
            response = await gateway.router.dispatch(
                Request(method, params, id=1), RpcContext(actor=actor, transport="http"),
            )
            assert response is not None
            return response.to_dict()

        denied = await call("device.connect", "agent:untrusted", device="fixture")
        assert "error" in denied
        first = await call("device.connect", "human:operator", device="fixture")
        assert "error" in first
        assert "fixture" in gateway.devices.quarantined
        assert gateway.device("fixture").state is DeviceState.FAULT
        cleared = await call(
            "device.clear_fault", "human:operator",
            device="fixture", reason="inspected power and cable",
        )
        assert cleared["result"]["state"] == "offline"
        assert "fixture" not in gateway.devices.quarantined
        again = await call("device.connect", "human:operator", device="fixture")
        assert "error" in again
        assert gateway.device("fixture").state is DeviceState.FAULT
    finally:
        await gateway.close()


async def test_dry_run_keeps_hardware_preconditions(tmp_path):
    scope = SimulatedMicroscope(
        DeviceDescriptor(id="scope", simulated=True),
        data_dir=str(tmp_path), seed=1,
    )
    await scope.connect()
    try:
        with pytest.raises(DeviceNotReady):
            await scope.invoke(
                "MotionControl", "move_absolute",
                {"x_um": 100.0, "y_um": 100.0},
                ExecutionContext(dry_run=True),
            )
    finally:
        await scope.disconnect()


async def test_core_mcp_catalog_smaller_than_full(tmp_path):
    gateway = Gateway(LabConfig(devices=[DeviceConfig(
        id="arm", driver="sim_robot_arm",
    )]), data_dir=tmp_path)
    await gateway.start()
    try:
        ctx = RpcContext(actor="agent:catalog")
        async def names(profile):
            reply = await mcp_router(gateway, profile=profile).dispatch(
                Request("tools/list", id=1), ctx,
            )
            return {tool["name"] for tool in reply.to_dict()["result"]["tools"]}

        core, full = await names("core"), await names("full")
        assert core < full
        assert {"hardware_analyze", "device_invoke", "device_simulate",
                "device_read", "estop"} <= core
        assert {"experiment_start", "campaign_start"} <= full
        assert "experiment_start" not in core
        assert "hardware_discover" not in core
        assert any(item.startswith("hcp_arm_") for item in core)
        alias = await mcp_router(gateway).dispatch(Request(
            "tools/call", {"name": "hardware_discover", "arguments": {}}, id=2,
        ), ctx)
        assert not alias.to_dict()["result"].get("isError", False)
    finally:
        await gateway.close()

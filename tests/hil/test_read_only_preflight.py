"""Explicitly opted-in, read-only certification preflight on physical hardware.

Never run by default, never send a mutating command and never assert that
a simulated test replaces operator-run emergency stop / disconnect testing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from labbench.core.registry import LabConfig
from labbench.gateway import Gateway

CONFIG = os.environ.get("HCP_HIL_CONFIG", "")
DEVICE = os.environ.get("HCP_HIL_DEVICE_ID", "")
ACK = os.environ.get("HCP_HIL_OPERATOR_ACK", "")

pytestmark = pytest.mark.skipif(
    not (CONFIG and DEVICE and ACK == "READ_ONLY_ON_PHYSICAL_HARDWARE"),
    reason="HIL requires HCP_HIL_CONFIG, HCP_HIL_DEVICE_ID and explicit operator acknowledgement",
)


@pytest.mark.asyncio
async def test_verified_physical_identity_and_read_only_telemetry(tmp_path: Path):
    config = LabConfig.load(CONFIG)
    selection = [device for device in config.devices if device.id == DEVICE]
    assert len(selection) == 1, "exactly one configured physical device is required"
    assert not selection[0].driver.startswith("sim_"), "HIL is for physical hardware"
    # Do not open or identify other configured endpoints.
    config.devices = selection
    config.scpi_targets = []
    config.scpi_bindings = []
    gateway = Gateway(config, data_dir=tmp_path)
    try:
        result = await gateway.start()
        assert result["connected"].get(DEVICE) == "idle", result
        assert DEVICE not in gateway.devices.quarantined
        dev = gateway.device(DEVICE)
        assert dev.descriptor.serial, "device must report a traceable serial identity"
        assert selection[0].expected_identity.get("serial") or (
            selection[0].settings.get("identity", {}).get("serial")
        ), "operator must pin the device identity in config"
        # No writes, commands, resets, or e-stop calls happen in this test.
        for feature in dev.features().values():
            for prop in feature.properties:
                if not prop.writable:
                    sample = await dev.read(feature.identifier, prop.name)
                    assert sample.device_id == DEVICE
    finally:
        await gateway.close()

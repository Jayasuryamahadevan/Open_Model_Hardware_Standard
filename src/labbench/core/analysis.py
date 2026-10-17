"""Evidence-based hardware connection and controllability report."""

from __future__ import annotations

import hashlib
import platform
from typing import Any, Mapping

from ..bridge.schema import sanitise_name
from .device import Device


def mcp_control_name(device_id: str, feature: str, command: str) -> str:
    identity = f"{device_id}.{feature}.{command}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return f"hcp_{sanitise_name(identity)[:50]}_{suffix}"


def _endpoint(
    source: str,
    address: str,
    *,
    evidence: dict[str, Any],
    protocol: str,
    probe: str,
    control_status: str = "unconfigured",
    binding: str | None = None,
    next_step: str,
) -> dict[str, Any]:
    return {
        "id": hashlib.sha256(f"{source}\0{address}".encode("utf-8")).hexdigest()[:16],
        "source": source,
        "protocol": protocol,
        "address": address,
        "probe": probe,
        "evidence": evidence,
        "control_status": control_status,
        "reviewed_binding": binding,
        "next_step": next_step,
    }


def build_analysis(
    inventory: dict[str, Any],
    devices: Mapping[str, Device],
    quarantined: Mapping[str, str],
) -> dict[str, Any]:
    """Create a report without opening ports or invoking hardware commands."""
    endpoints: list[dict[str, Any]] = []
    bindings = {item["address"]: item for item in inventory.get("bindings", [])}
    for item in inventory.get("usb", []):
        ports = item.get("serial_ports", [])
        endpoints.append(_endpoint(
            "usb", item["bus_path"], protocol="usb-descriptor", probe="passive",
            evidence={key: item.get(key) for key in (
                "vendor_id", "product_id", "vendor", "model", "serial", "serial_ports"
            )},
            next_step=(
                "Inspect the listed serial ports and match an installed, reviewed driver."
                if ports else
                "USB identity alone does not expose a safe control API; install a device driver."
            ),
        ))
    for item in inventory.get("serial", []):
        endpoints.append(_endpoint(
            "serial", item["port"], protocol="serial", probe="passive",
            evidence={key: item.get(key) for key in (
                "vendor_id", "product_id", "vendor", "model", "serial"
            )},
            next_step="Serial framing and commands require an explicit device-specific driver.",
        ))
    for item in inventory.get("visa", []):
        if "address" not in item:
            continue
        endpoints.append(_endpoint(
            "visa", item["address"], protocol="visa", probe="passive",
            evidence={"resource": item["address"]},
            next_step="Verify the resource and command profile before enabling a VISA driver.",
        ))
    for item in inventory.get("scpi", []):
        address = item["address"]
        binding = bindings.get(address)
        ready = bool(binding and binding["ready_to_activate"])
        endpoints.append(_endpoint(
            "scpi", address, protocol="scpi", probe="explicit-idn-query",
            evidence={key: item[key] for key in (
                "vendor", "model", "serial", "firmware", "identity_reply", "error"
            ) if key in item},
            control_status=(
                "reviewed_binding_verified" if ready else
                "identity_observed" if "identity_reply" in item else "probe_failed"
            ),
            binding=binding["rule_id"] if ready else None,
            next_step=(
                f"Review manual and activate binding {binding['rule_id']!r}."
                if ready else
                "An identity response is not a command schema. Configure an exact identity "
                "binding and reviewed command profile before activation."
            ),
        ))

    configured: list[dict[str, Any]] = []
    tools_available = 0
    for device_id, device in sorted(devices.items()):
        eligible = device_id not in quarantined and device.state.operational
        features: list[dict[str, Any]] = []
        for feature in device.features().values():
            commands = []
            for command in feature.commands:
                if "raw" in command.tags:
                    continue
                commands.append({
                    "name": command.name,
                    "description": command.description,
                    "input_schema": command.input_schema(),
                    "hazard": command.hazard.value,
                    "reversibility": command.reversibility.value,
                    "mcp_tool": mcp_control_name(device_id, feature.identifier, command.name)
                    if eligible else None,
                    "rpc_method": "device.invoke",
                })
                if eligible:
                    tools_available += 1
            properties = [item.name for item in feature.properties]
            features.append({"name": feature.identifier, "properties": properties,
                             "commands": commands})
        configured.append({
            "id": device_id,
            "descriptor": device.descriptor.model_dump(mode="json"),
            "state": device.state.value,
            "quarantine_reason": quarantined.get(device_id),
            "controllable": eligible,
            "features": features,
        })

    verified = sum(1 for item in endpoints
                   if item["control_status"] == "reviewed_binding_verified")
    inventory_only = sum(1 for item in endpoints
                         if item["control_status"] != "reviewed_binding_verified")
    return {
        "version": "1.0",
        "host": {"os": platform.system(), "architecture": platform.machine()},
        "policy": {
            "scanning": "passive enumeration plus *IDN? to explicitly configured SCPI addresses",
            "auto_activation": False,
            "unreviewed_control": False,
            "unrecognized_hardware": "inventory_only",
        },
        "endpoints": endpoints,
        "devices": configured,
        "summary": {
            "observed_endpoints": len(endpoints),
            "unreviewed_endpoints": inventory_only,
            "verified_bindings": verified,
            "configured_devices": len(configured),
            "controllable_devices": sum(item["controllable"] for item in configured),
            "published_device_command_tools": tools_available,
        },
        "next_steps": [
            "Review endpoint identity and select a supported driver/profile.",
            "Use hardware.manual to inspect reviewed binding documentation.",
            "Use hardware.activate only for ready_to_activate bindings.",
            "Call tools/list again after activation to receive typed MCP commands.",
            "Simulate supported actions when possible; obey approval decisions and "
            "physical safety interlocks.",
        ],
        "limitations": [
            "Only hardware with an accessible host interface and implemented driver can be controlled.",
            "USB, serial and VISA enumeration does not establish a supported command language.",
            "Neither a device identity nor a manual proves that commands are physically safe.",
            "Discovery cannot override the gateway's safety policy or device emergency stop.",
        ],
    }

"""Read-only laptop hardware inventory and bounded SCPI identification.

USB descriptors identify a physical connection, not a command language. A
SCPI *IDN? reply identifies an instrument, not its operating limits. This
module deliberately returns evidence and enrollment guidance rather than
manufacturing unsafe capabilities from a product string.
"""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
from pathlib import Path
from typing import Any


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except (OSError, UnicodeError):
        return None


def usb_inventory(sysfs: Path = Path("/sys/bus/usb/devices"),
                  serial_dir: Path = Path("/dev/serial/by-id")) -> list[dict[str, Any]]:
    """Enumerate USB devices through Linux sysfs without opening a device."""
    serial_links: dict[str, list[str]] = {}
    if serial_dir.exists():
        for link in serial_dir.iterdir():
            try:
                target = link.resolve(strict=True)
            except OSError:
                continue
            serial_links.setdefault(str(target), []).append(str(link))
    out: list[dict[str, Any]] = []
    if not sysfs.exists():
        return out
    for node in sorted(sysfs.iterdir()):
        vendor_id, product_id = _read(node / "idVendor"), _read(node / "idProduct")
        if not vendor_id or not product_id:
            continue
        usb_serial = _read(node / "serial")
        ports: list[str] = []
        # ttyUSB/ttyACM interfaces are nested below the USB device node.
        for tty in node.glob("**/tty/tty*"):
            dev_path = Path("/dev") / tty.name
            ports.append(str(dev_path))
            ports.extend(serial_links.get(str(dev_path), []))
        out.append({
            "source": "usb-sysfs", "bus_path": node.name,
            "vendor_id": vendor_id.lower(), "product_id": product_id.lower(),
            "vendor": _read(node / "manufacturer"),
            "model": _read(node / "product"), "serial": usb_serial,
            "serial_ports": sorted(set(ports)),
            "identification": "usb_descriptor",
            "control_status": "unconfigured",
            "next_step": "Match an installed driver and review its command profile and safety limits.",
        })
    return out


def serial_inventory() -> list[dict[str, Any]]:
    """Cross-platform USB/serial descriptors when pyserial is installed."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [
        {"source": "serial-port", "port": port.device, "vendor_id":
         f"{port.vid:04x}" if port.vid is not None else None,
         "product_id": f"{port.pid:04x}" if port.pid is not None else None,
         "vendor": port.manufacturer, "model": port.product,
         "serial": port.serial_number, "identification": "port_descriptor",
         "control_status": "unconfigured"}
        for port in list_ports.comports()
    ]


def visa_inventory() -> list[dict[str, Any]]:
    """List VISA resource addresses without opening or querying them."""
    try:
        import pyvisa
    except ImportError:
        return []
    try:
        manager = pyvisa.ResourceManager()
        try:
            return [{"source": "visa", "address": address,
                     "control_status": "unconfigured"}
                    for address in manager.list_resources()]
        finally:
            manager.close()
    except Exception as exc:  # noqa: BLE001 - missing VISA backend is a discovery result
        return [{"source": "visa", "error": str(exc)}]


async def scpi_identity(address: str, timeout_s: float = 2.0) -> dict[str, Any]:
    """Send only *IDN? to an explicitly configured TCP endpoint."""
    from ..drivers.scpi import ScpiTransport, VisaTransport, _looks_like_visa

    if _looks_like_visa(address):
        transport = VisaTransport(address, timeout_s=timeout_s)
    else:
        host, sep, port_text = address.rpartition(":")
        if not sep or not host or not port_text.isdigit():
            return {"source": "scpi", "address": address, "error":
                    "expected host:port or a VISA resource address"}
        port = int(port_text)
        if port < 1 or port > 65535:
            return {"source": "scpi", "address": address, "error": "invalid port"}
        transport = ScpiTransport(host, port, timeout_s=timeout_s)
    try:
        await transport.open()
        identity = await transport.query("*IDN?")
    except Exception as exc:  # noqa: BLE001 - a probe failure belongs in the inventory
        return {"source": "scpi", "address": address, "error": str(exc)}
    finally:
        await transport.close()
    fields = [part.strip() for part in identity.split(",")]
    vendor, model, serial, firmware = (fields + [None] * 4)[:4]
    return {
        "source": "scpi", "address": address, "identity_reply": identity,
        "vendor": vendor, "model": model, "serial": serial, "firmware": firmware,
        "identification": "scpi_idn", "control_status": "unconfigured",
        "next_step": "Choose and review a SCPI command profile before adding this device to the lab config.",
    }


def _native_query(command: list[str], timeout_s: float = 8.0) -> Any:
    """Run one fixed, passive OS inventory command with an execution deadline."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if result.returncode:
        return {"error": result.stderr[:300] or f"exit status {result.returncode}"}
    if len(result.stdout) > 4_000_000:
        return {"error": "native inventory exceeded 4 MB"}
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return {"error": "native inventory was not valid JSON"}


def _mac_usb_nodes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            vendor = node.get("vendor_id", "")
            product = node.get("product_id", "")
            if vendor or product:
                found.append({
                    "source": "macos-system-profiler", "bus_path": node.get("_name", ""),
                    "vendor_id": str(vendor), "product_id": str(product),
                    "vendor": node.get("manufacturer"), "model": node.get("_name"),
                    "serial": node.get("serial_num"), "control_status": "unconfigured",
                    "identification": "usb_descriptor",
                })
            children = node.get("_items")
            if isinstance(children, list):
                walk(children)

    walk(items)
    return found


def native_inventory(os_name: str | None = None) -> list[dict[str, Any]]:
    """Enumerate native devices on Windows/macOS without opening control channels.

    Linux sysfs and cross-platform pyserial/VISA are separately inventoried.
    OS commands are fixed, bounded and never include agent-supplied fragments.
    """
    system = os_name or platform.system()
    if system == "Darwin":
        data = _native_query(["system_profiler", "SPUSBDataType", "-json"])
        if isinstance(data, dict) and isinstance(data.get("SPUSBDataType"), list):
            return _mac_usb_nodes(data["SPUSBDataType"])
        return [{"source": "macos-system-profiler", "error": data.get("error", "unexpected schema")
                 if isinstance(data, dict) else "unexpected response"}]
    if system == "Windows":
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            ("Get-PnpDevice -PresentOnly | "
             "Select-Object InstanceId,Class,FriendlyName,Manufacturer,Status | "
             "ConvertTo-Json -Compress -Depth 3"),
        ]
        data = _native_query(command)
        if isinstance(data, dict) and "error" in data:
            return [{"source": "windows-pnp", "error": data["error"]}]
        records = data if isinstance(data, list) else [data]
        return [{
            "source": "windows-pnp", "bus_path": str(entry.get("InstanceId", "")),
            "class": entry.get("Class"), "vendor": entry.get("Manufacturer"),
            "model": entry.get("FriendlyName"), "status": entry.get("Status"),
            "control_status": "unconfigured", "identification": "pnp_descriptor",
        } for entry in records if isinstance(entry, dict) and entry.get("InstanceId")]
    return []


async def discover_hardware(scpi_targets: list[str] | None = None,
                            *, sysfs: Path = Path("/sys/bus/usb/devices"),
                            serial_dir: Path = Path("/dev/serial/by-id")) -> dict[str, Any]:
    targets = list(dict.fromkeys(scpi_targets or []))
    network = await asyncio.gather(*(scpi_identity(target) for target in targets))
    native, serial, visa = await asyncio.gather(
        asyncio.to_thread(native_inventory),
        asyncio.to_thread(serial_inventory),
        asyncio.to_thread(visa_inventory),
    )
    return {"usb": usb_inventory(sysfs, serial_dir), "native": native,
            "serial": serial, "visa": visa, "scpi": network,
            "note": "Discovery is identification only. Commands require an installed, reviewed driver and safety profile."}

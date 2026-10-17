"""Driver plugin registry and device lifecycle management.

Drivers are discovered through the ``labbench.drivers`` entry-point group, so a
third party ships a driver as an ordinary pip package and it appears here with
no edit to LabBench. That is the "plug in any hardware" property: the contract
is the `Device` ABC plus an entry point, nothing more.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata as md
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .device import Device, DeviceDescriptor, DeviceState
from .network_discovery import NetworkTarget
from .errors import DeviceNotFound, DriverUnavailable

ENTRY_POINT_GROUP = "labbench.drivers"


class DeviceConfig(BaseModel):
    """One entry in the lab configuration file."""

    model_config = ConfigDict(extra="forbid")

    id: str
    driver: str
    display_name: str = ""
    kind: str = "instrument"
    vendor: str = "unknown"
    model: str = "unknown"
    serial: str | None = None
    location: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    #: Connect at startup. Set false for instruments that must be woken manually.
    autoconnect: bool = True
    #: Driver-specific settings passed through verbatim.
    settings: dict[str, Any] = Field(default_factory=dict)
    #: Operator-provided manuals. Local files and HTTPS links are exposed to agents.
    manuals: list[str] = Field(default_factory=list)
    #: Exact identity fields to verify after the driver connects.
    expected_identity: dict[str, str] = Field(default_factory=dict)

    @field_validator("expected_identity")
    @classmethod
    def validate_expected_identity(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - {"vendor", "model", "serial", "firmware"}
        if unknown:
            raise ValueError(f"unsupported identity fields: {sorted(unknown)}")
        if any(not item.strip() for item in value.values()):
            raise ValueError("expected identity values must not be blank")
        return value


class ScpiBinding(BaseModel):
    """A reviewed, exact identity rule for attaching a discovered SCPI device."""

    model_config = ConfigDict(extra="forbid")

    id: str
    address: str
    vendor: str
    model: str
    profile: str
    serial: str | None = None
    firmware: str | None = None
    kind: str = "instrument"
    display_name: str = ""
    manuals: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("vendor", "model", "profile")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("binding identity and profile must not be blank")
        return value


class LabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "lab"
    description: str = ""
    devices: list[DeviceConfig] = Field(default_factory=list)
    #: Explicit SCPI addresses to identify with *IDN?; never a subnet scan.
    scpi_targets: list[str] = Field(default_factory=list)
    #: Devices may be activated at runtime only after exact identity matching.
    scpi_bindings: list[ScpiBinding] = Field(default_factory=list)
    #: Explicit local Wi-Fi/LAN endpoints; no network or subnet scanning.
    network_targets: list[NetworkTarget] = Field(default_factory=list)
    #: Raw safety policy; parsed by labbench.core.safety.SafetyPolicy.
    safety: dict[str, Any] = Field(default_factory=dict)
    memory: list[dict[str, Any]] = Field(default_factory=list)
    #: Named identities for the HTTP/WebSocket transports, parsed by
    #: labbench.protocol.auth.Credential. Superset of the legacy --token flag:
    #: once any are configured, the actor written to the ledger is always the
    #: matched credential's, never a caller-supplied header. See
    #: protocol/auth.py for why that distinction is load-bearing.
    credentials: list[dict[str, Any]] = Field(default_factory=list)
    data_dir: str = "./labbench-data"

    @classmethod
    def load(cls, path: str | Path) -> LabConfig:
        config_path = Path(path).expanduser().resolve()
        text = config_path.read_text(encoding="utf-8")
        config = cls.model_validate(yaml.safe_load(text) or {})
        for device in config.devices:
            device.manuals = [
                source if source.startswith("https://") or Path(source).is_absolute()
                else str((config_path.parent / source).resolve())
                for source in device.manuals
            ]
        for binding in config.scpi_bindings:
            binding.manuals = [
                source if source.startswith("https://") or Path(source).is_absolute()
                else str((config_path.parent / source).resolve())
                for source in binding.manuals
            ]
        return config


class DriverRegistry:
    """Maps driver names to `Device` subclasses."""

    def __init__(self) -> None:
        self._drivers: dict[str, type[Device]] = {}
        self._failed: dict[str, str] = {}
        self._loaded = False

    def discover(self, *, force: bool = False) -> dict[str, type[Device]]:
        if self._loaded and not force:
            return self._drivers
        for ep in md.entry_points(group=ENTRY_POINT_GROUP):
            try:
                cls = ep.load()
            except Exception as exc:  # noqa: BLE001 - optional dependency missing, usually
                self._failed[ep.name] = f"{type(exc).__name__}: {exc}"
                continue
            if not (isinstance(cls, type) and issubclass(cls, Device)):
                self._failed[ep.name] = "entry point is not a Device subclass"
                continue
            self._drivers[ep.name] = cls
        self._loaded = True
        return self._drivers

    def register(self, name: str, cls: type[Device]) -> None:
        self._drivers[name] = cls

    def get(self, name: str) -> type[Device]:
        self.discover()
        if name in self._drivers:
            return self._drivers[name]
        if name in self._failed:
            raise DriverUnavailable(
                f"driver {name!r} is installed but failed to load: {self._failed[name]}. "
                f"It likely needs an optional dependency: pip install 'labbench[{name}]'",
                driver=name, cause=self._failed[name],
            )
        # Allow a fully-qualified path for drivers not shipped as entry points.
        if ":" in name:
            mod_name, _, cls_name = name.partition(":")
            try:
                cls = getattr(importlib.import_module(mod_name), cls_name)
            except Exception as exc:
                raise DriverUnavailable(
                    f"cannot import driver {name!r}: {exc}", driver=name
                ) from exc
            self._drivers[name] = cls
            return cls
        raise DriverUnavailable(
            f"unknown driver {name!r}; available: {sorted(self._drivers)}",
            driver=name, available=sorted(self._drivers),
            unavailable=self._failed,
        )

    def catalog(self) -> dict[str, Any]:
        self.discover()
        return {
            "available": sorted(self._drivers),
            "unavailable": dict(self._failed),
        }


class DeviceManager:
    """Instantiates, connects and tracks the lab's devices."""

    def __init__(self, registry: DriverRegistry | None = None) -> None:
        self.registry = registry or DriverRegistry()
        self._devices: dict[str, Device] = {}
        self._configs: dict[str, DeviceConfig] = {}
        self._event_sinks: list[Any] = []
        self.quarantined: dict[str, str] = {}

    # -- construction -----------------------------------------------------

    def add_from_config(self, cfg: DeviceConfig) -> Device:
        if cfg.id in self._devices:
            raise ValueError(f"device {cfg.id!r} is already registered")
        cls = self.registry.get(cfg.driver)
        descriptor = DeviceDescriptor(
            id=cfg.id,
            display_name=cfg.display_name or cfg.id,
            kind=cfg.kind, vendor=cfg.vendor, model=cfg.model,
            serial=cfg.serial, location=cfg.location, labels=cfg.labels,
            driver=cfg.driver,
            simulated=cfg.driver.startswith("sim_"),
        )
        device = cls(descriptor, **cfg.settings)
        self._devices[cfg.id] = device
        self._configs[cfg.id] = cfg
        for sink in self._event_sinks:
            device.subscribe(sink)
        return device

    def remove(self, device_id: str) -> None:
        self._devices.pop(device_id, None)
        self._configs.pop(device_id, None)
        self.quarantined.pop(device_id, None)

    def load(self, config: LabConfig) -> list[Device]:
        return [self.add_from_config(c) for c in config.devices]

    def subscribe_all(self, sink: Any) -> None:
        """Attach an event sink to current and future devices."""
        self._event_sinks.append(sink)
        for device in self._devices.values():
            device.subscribe(sink)

    # -- access -----------------------------------------------------------

    def get(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFound(
                f"no device {device_id!r} in this lab",
                device=device_id, available=sorted(self._devices),
            ) from None

    def all(self) -> dict[str, Device]:
        return dict(self._devices)

    def find(
        self, *, kind: str | None = None, feature: str | None = None,
        label: tuple[str, str] | None = None, state: DeviceState | None = None,
    ) -> list[Device]:
        """Capability-based lookup — 'give me something that can image'.

        This is how an agent stays vendor-agnostic: it asks for a feature, not
        for a model number.
        """
        out = []
        for d in self._devices.values():
            if kind is not None and d.descriptor.kind != kind:
                continue
            if feature is not None and feature not in d.features():
                continue
            if label is not None and d.descriptor.labels.get(label[0]) != label[1]:
                continue
            if state is not None and d.state is not state:
                continue
            out.append(d)
        return out

    # -- lifecycle --------------------------------------------------------

    async def connect_all(self) -> dict[str, str]:
        """Connect every autoconnect device, tolerating individual failures.

        One dead instrument must not prevent the rest of the lab coming up.
        """
        results: dict[str, str] = {}

        async def one(dev: Device) -> None:
            results[dev.id] = await self.connect_one(dev.id)

        await asyncio.gather(*(one(d) for d in self._devices.values()))
        return results

    async def connect_one(self, device_id: str) -> str:
        dev = self.get(device_id)
        cfg = self._configs.get(device_id)
        if cfg is not None and not cfg.autoconnect:
            return "skipped (autoconnect: false)"
        try:
            await dev.connect()
            expected = cfg.expected_identity if cfg is not None else {}
            mismatches = [
                f"{field}: expected {value!r}, observed {getattr(dev.descriptor, field, None)!r}"
                for field, value in expected.items()
                if getattr(dev.descriptor, field, None) != value
            ]
            if mismatches:
                reason = "identity mismatch: " + "; ".join(mismatches)
                self.quarantined[dev.id] = reason
                await dev.disconnect()
                await dev.fail(reason)
                return f"error: {reason}"
            return dev.state.value
        except Exception as exc:  # noqa: BLE001 - caller reports one failed device
            return f"error: {exc}"

    async def disconnect_all(self) -> None:
        await asyncio.gather(
            *(d.disconnect() for d in self._devices.values()),
            return_exceptions=True,
        )

    async def estop_all(self, reason: str = "global e-stop") -> dict[str, str]:
        """Stop every device. Failures are reported, never swallowed."""
        results: dict[str, str] = {}

        async def one(dev: Device) -> None:
            try:
                await dev.estop(reason)
                results[dev.id] = "stopped"
            except Exception as exc:  # noqa: BLE001 - a failed e-stop must be reported, never raised
                results[dev.id] = f"ESTOP FAILED: {exc}"

        await asyncio.gather(*(one(d) for d in self._devices.values()))
        return results

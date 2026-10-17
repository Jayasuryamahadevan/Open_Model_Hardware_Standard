"""Operator-reviewed USB CDC/serial ASCII driver.

Only an exact OS USB identity plus a bounded read-only identity query may
activate this adapter. All executable frames are enumerated in trusted local
configuration; AI clients cannot send raw bytes or select serial endpoints.
"""

from __future__ import annotations

import asyncio
import re
import string
import threading
from typing import Any

from ..core.capability import Command, Feature, Hazard, Property
from ..core.device import Device, DeviceDescriptor, ExecutionContext
from ..core.errors import DeviceFault, TransportError, ValidationError

_SAFE_FRAME = re.compile(r"^[A-Za-z0-9_ .,:?*+\-/={}]+$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.+\-]+$")


def _frame(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or not _SAFE_FRAME.fullmatch(value):
        raise ValidationError("serial command must be a bounded printable ASCII frame")
    return value


def _template(value: str, names: set[str]) -> str:
    _frame(value)
    for _, name, fmt, conversion in string.Formatter().parse(value):
        if name is None:
            continue
        if name not in names or fmt or conversion:
            raise ValidationError("serial templates may only reference declared parameters")
    return value


def _usb_identity(port: str) -> dict[str, Any]:
    from serial.tools import list_ports

    for item in list_ports.comports():
        if item.device == port or item.name == port:
            return {
                "vid": item.vid, "pid": item.pid,
                "serial_number": item.serial_number,
            }
    raise TransportError("configured serial port is not enumerated")


class SerialAsciiDevice(Device):
    """USB/serial devices with explicit identity and typed command vocabulary."""

    requires_package = "serial"

    def __init__(self, descriptor: DeviceDescriptor, **config: Any) -> None:
        super().__init__(descriptor, **config)
        self.port = config.get("port", "")
        expected = config.get("usb_identity", {})
        if not isinstance(self.port, str) or not self.port or not isinstance(expected, dict):
            raise ValidationError("serial_ascii needs a port and usb_identity")
        if not all(key in expected for key in ("vid", "pid", "serial_number")):
            raise ValidationError("serial_ascii must pin VID, PID and USB serial number")
        self.usb_identity = expected
        self.baudrate = int(config.get("baudrate", 9600))
        if not 300 <= self.baudrate <= 4_000_000:
            raise ValidationError("unsupported baudrate")
        self.timeout_s = float(config.get("timeout_s", 1.0))
        if not 0.1 <= self.timeout_s <= 10:
            raise ValidationError("serial timeout must be in 0.1..10 seconds")
        self.terminator = config.get("terminator", "\\n")
        if self.terminator not in ("\\n", "\\r", "\\r\\n"):
            raise ValidationError("serial terminator must be LF, CR or CRLF")
        self.identity_query = _frame(config.get("identity_query", "*IDN?"))
        self.identity_reply = _frame(config.get("identity_reply", ""))
        self.stop_frame = (_frame(config["stop_frame"])
                           if config.get("stop_frame") else None)
        self._lock_sync = threading.Lock()
        self._serial: Any = None
        self._features_def: list[Feature] = []
        self._queries: dict[tuple[str, str], str] = {}
        self._commands: dict[tuple[str, str], str] = {}
        for item in config.get("features", []):
            fid = item["identifier"]
            properties: list[Property] = []
            commands: list[Command] = []
            for prop in item.get("properties", []):
                parsed = Property.model_validate({
                    "name": prop["name"], "description": prop.get("description", ""),
                    "schema": prop["schema"], "writable": False,
                })
                key = (fid, parsed.name)
                if key in self._queries:
                    raise ValidationError("duplicate serial property")
                self._queries[key] = _frame(prop["query"])
                properties.append(parsed)
            for cfg in item.get("commands", []):
                if "hazard" not in cfg or "reversibility" not in cfg:
                    raise ValidationError("serial commands require hazard and reversibility")
                cmd = Command.model_validate({
                    key: value for key, value in cfg.items() if key != "frame"
                })
                if cmd.hazard.rank >= Hazard.MOTION.rank and not self.stop_frame:
                    raise ValidationError("actuating serial commands require a declared stop frame")
                if cmd.hazard is Hazard.NONE and not cfg["frame"].endswith("?"):
                    raise ValidationError("only queries may be declared hazard none")
                if any(p.type not in ("number", "integer", "boolean", "string")
                       or (p.type == "string" and p.constraint.enum is None)
                       for p in cmd.parameters):
                    raise ValidationError("serial parameters must be scalar; strings require enums")
                key = (fid, cmd.name)
                if key in self._commands:
                    raise ValidationError("duplicate serial command")
                self._commands[key] = _template(cfg["frame"], {
                    p.name for p in cmd.parameters
                })
                commands.append(cmd)
            self._features_def.append(Feature(
                identifier=fid, description=item.get("description", ""),
                properties=properties, commands=commands,
            ))
        if not self._features_def:
            raise ValidationError("serial_ascii requires a reviewed feature profile")

    def _features(self) -> list[Feature]:
        return self._features_def

    def _exchange(self, message: str, *, query: bool) -> str:
        if self._serial is None:
            raise TransportError("serial endpoint disconnected", device=self.id)
        payload = (message + self.terminator).encode("ascii")
        with self._lock_sync:
            try:
                self._serial.write(payload)
                self._serial.flush()
                if not query:
                    return ""
                reply = self._serial.readline(4097)
                if len(reply) > 4096 or not reply:
                    raise TransportError("serial identity/query timed out or exceeded 4096 bytes",
                                         device=self.id)
                return reply.decode("ascii").strip()
            except (UnicodeError, OSError) as exc:
                raise TransportError("serial communication failed", device=self.id) from exc

    async def _connect(self) -> None:
        import serial

        observed = await asyncio.to_thread(_usb_identity, self.port)
        for field in ("vid", "pid", "serial_number"):
            if observed[field] != self.usb_identity[field]:
                raise ValidationError(f"USB descriptor mismatch: {field}")
        self._serial = serial.Serial(
            self.port, baudrate=self.baudrate, timeout=self.timeout_s,
            write_timeout=self.timeout_s,
        )
        try:
            reply = await asyncio.to_thread(self._exchange, self.identity_query, query=True)
            if reply != self.identity_reply:
                raise ValidationError("serial identity response mismatch")
            self.descriptor.serial = observed["serial_number"]
            self.descriptor.protocol = "serial_ascii"
        except BaseException:
            await self._disconnect()
            raise

    async def _disconnect(self) -> None:
        opened, self._serial = self._serial, None
        if opened is not None:
            await asyncio.to_thread(opened.close)

    async def _read(self, feature: str, name: str) -> Any:
        return await asyncio.to_thread(
            self._exchange, self._queries[(feature, name)], query=True,
        )

    async def _invoke(
        self, feature: str, command: str, args: dict[str, Any], ctx: ExecutionContext,
    ) -> dict[str, Any]:
        template = self._commands[(feature, command)]
        safe = {}
        for key, value in args.items():
            formatted = str(int(value)) if isinstance(value, bool) else str(value)
            if not _SAFE_VALUE.fullmatch(formatted):
                raise ValidationError("serial parameter contains unsupported frame characters")
            safe[key] = formatted
        message = _frame(template.format_map(safe))
        result = await asyncio.to_thread(
            self._exchange, message, query=message.endswith("?"),
        )
        return {"reply": result, "sent": True}

    async def _estop(self) -> None:
        if not self.stop_frame:
            raise DeviceFault("no serial stop frame; use independent physical stop",
                              device=self.id)
        await asyncio.to_thread(self._exchange, self.stop_frame, query=False)

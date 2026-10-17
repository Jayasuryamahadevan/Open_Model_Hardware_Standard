"""Reviewed REST/JSON device adapter with explicit command allowlists.

This driver never discovers executable HTTP operations from untrusted device
responses. Operators provide literal relative paths, typed parameters, command
hazards, and the expected device identity. There is no arbitrary URL tool.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from ..core.capability import Command, Feature, Hazard, Property
from ..core.device import Device, DeviceDescriptor, ExecutionContext
from ..core.errors import DeviceFault, TransportError, ValidationError


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or value.startswith("//") or ".." in value.split("/")
            or any(s in value for s in ("?", "#", "%", "\\", "://"))
            or any(ord(c) < 32 for c in value)):
        raise ValidationError("HTTP device paths must be literal relative paths beginning with /")
    return value


class HttpApiDevice(Device):
    """HTTP/JSON hardware with operator-reviewed routes and bounded requests."""

    requires_package = "httpx"

    def __init__(self, descriptor: DeviceDescriptor, **config: Any) -> None:
        super().__init__(descriptor, **config)
        url = config.get("base_url", "")
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValidationError("http_api requires an http(s) base_url without credentials/query")
        self.base_url = url.rstrip("/")
        self.timeout_s = float(config.get("timeout_s", 5))
        if not 0.1 <= self.timeout_s <= 60:
            raise ValidationError("http_api timeout_s must be between 0.1 and 60")
        self.token_env = config.get("token_env")
        self.identity_path = _path(config.get("identity_path", "/identity"))
        self.expected = config.get("identity", {})
        if not isinstance(self.expected, dict) or not self.expected.get("serial"):
            raise ValidationError("http_api requires an exact expected identity.serial")
        if not all(isinstance(v, str) and v for v in self.expected.values()):
            raise ValidationError("http_api expected identity values must be non-empty strings")
        self.estop_path = _path(config["estop_path"]) if config.get("estop_path") else None
        self._client: Any = None
        self._properties: dict[tuple[str, str], str] = {}
        self._commands: dict[tuple[str, str], tuple[str, str]] = {}
        self._feature_defs: list[Feature] = []
        for item in config.get("features", []):
            identifier = item.get("identifier")
            if not isinstance(identifier, str) or not identifier:
                raise ValidationError("http_api feature requires an identifier")
            properties: list[Property] = []
            commands: list[Command] = []
            for entry in item.get("properties", []):
                prop = Property.model_validate({
                    "name": entry["name"], "schema": entry["schema"],
                    "description": entry.get("description", ""),
                })
                properties.append(prop)
                key = (identifier, prop.name)
                if key in self._properties:
                    raise ValidationError("duplicate http_api property")
                self._properties[key] = _path(entry["path"])
            for entry in item.get("commands", []):
                if "hazard" not in entry or "reversibility" not in entry:
                    raise ValidationError("http_api commands require explicit hazard and reversibility")
                method = entry.get("method", "POST").upper()
                if method not in ("GET", "POST", "PUT"):
                    raise ValidationError("http_api permits only GET, POST and PUT")
                command = Command.model_validate({
                    key: value for key, value in entry.items()
                    if key not in ("path", "method")
                })
                if method != "GET" and command.hazard is Hazard.NONE:
                    raise ValidationError("mutating HTTP commands may not have hazard=none")
                if method == "GET" and command.hazard is not Hazard.NONE:
                    raise ValidationError("GET commands must be declared read-only")
                if command.hazard.rank >= Hazard.MOTION.rank and self.estop_path is None:
                    raise ValidationError("hazardous HTTP commands require a configured stop endpoint")
                key = (identifier, command.name)
                if key in self._commands:
                    raise ValidationError("duplicate http_api command")
                self._commands[key] = (method, _path(entry["path"]))
                commands.append(command)
            self._feature_defs.append(Feature(
                identifier=identifier, properties=properties, commands=commands,
                description=item.get("description", ""),
            ))
        if not self._feature_defs:
            raise ValidationError("http_api needs at least one reviewed feature")

    def _features(self) -> list[Feature]:
        return self._feature_defs

    async def _connect(self) -> None:
        import os

        import httpx

        if self.token_env and not isinstance(self.token_env, str):
            raise ValidationError("token_env must name an environment variable")
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        if self.token_env and not token:
            raise ValidationError("http_api credential environment variable is unset")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s), headers=headers,
            follow_redirects=False, trust_env=False,
        )
        try:
            identity = await self._request("GET", self.identity_path)
            if not isinstance(identity, dict) or any(
                identity.get(key) != value for key, value in self.expected.items()
            ):
                raise ValidationError("http_api identity mismatch; device must stay offline")
            self.descriptor.vendor = str(identity.get("vendor", self.descriptor.vendor))
            self.descriptor.model = str(identity.get("model", self.descriptor.model))
            self.descriptor.serial = str(identity["serial"])
            self.descriptor.protocol = "http_api"
        except BaseException:
            await self._disconnect()
            raise

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, args: Any = None) -> Any:
        if self._client is None:
            raise TransportError("http_api client is disconnected", device=self.id)
        url = self.base_url + path
        try:
            response = await asyncio.wait_for(
                self._client.request(method, url, json=args if method != "GET" else None),
                timeout=self.timeout_s + 0.5,
            )
            if response.is_redirect:
                raise TransportError("http_api redirects are prohibited", device=self.id)
            response.raise_for_status()
            if len(response.content) > 1_048_576:
                raise TransportError("http_api response exceeds 1 MiB", device=self.id)
            return response.json() if response.content else {}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, (TransportError, DeviceFault)):
                raise
            raise TransportError(
                f"http_api {method} {path} failed: {type(exc).__name__}",
                device=self.id, state_uncertain=(method != "GET"),
            ) from exc

    async def _read(self, feature: str, name: str) -> Any:
        value = await self._request("GET", self._properties[(feature, name)])
        return value.get("value", value) if isinstance(value, dict) else value

    async def _invoke(
        self, feature: str, command: str, args: dict[str, Any], ctx: ExecutionContext,
    ) -> dict[str, Any]:
        method, path = self._commands[(feature, command)]
        result = await self._request(method, path, args)
        return result if isinstance(result, dict) else {"value": result}

    async def _estop(self) -> None:
        if self.estop_path is None:
            raise DeviceFault(
                "no remote stop endpoint; operator must use physical emergency stop",
                device=self.id,
            )
        await self._request("POST", self.estop_path, {})

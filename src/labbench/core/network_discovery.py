"""Bounded, opt-in discovery of explicitly configured LAN/Wi-Fi hardware.

Never scans IP ranges and never turns network discovery into executable tool
calls. Identity can be probed with one GET to an operator-approved address;
real control still needs a separate identity-verified driver and safety profile.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NetworkTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    protocol: str = "http-json"
    probe: bool = False
    identity_path: str = "/identity"
    expected_identity: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def trusted_url(cls, value: str) -> str:
        p = urllib.parse.urlsplit(value)
        if (p.scheme not in ("http", "https") or not p.hostname or p.username
                or p.password or p.path not in ("", "/") or p.query or p.fragment):
            raise ValueError("network target must be an origin URL without credentials/path")
        try:
            ip = ipaddress.ip_address(p.hostname)
        except ValueError:
            if not p.hostname.endswith(".local"):
                raise ValueError("network target must use a local IP or .local hostname") from None
        else:
            if not (ip.is_private or ip.is_loopback or ip.is_link_local):
                raise ValueError("network target must use private, loopback or link-local IP")
        try:
            port = p.port
        except ValueError as exc:
            raise ValueError("invalid network target port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid network port")
        return value.rstrip("/")

    @field_validator("identity_path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (not value.startswith("/") or value.startswith("//")
                or ".." in value or any(ch in value for ch in ("?", "#", "%", "\\"))):
            raise ValueError("identity_path must be a fixed relative URL path")
        return value

    @field_validator("expected_identity")
    @classmethod
    def pin_serial(cls, value: dict[str, str]) -> dict[str, str]:
        if value and (not value.get("serial") or any(not item for item in value.values())):
            raise ValueError("expected identity must pin a non-empty serial")
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        return None


def _probe(target: NetworkTarget) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        request = urllib.request.Request(
            target.url + target.identity_path,
            method="GET", headers={"Accept": "application/json"},
        )
        with opener.open(request, timeout=2.0) as response:
            if response.status != 200:
                raise ValueError(f"identity HTTP status {response.status}")
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError("identity exceeds 4 KiB")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("identity must be a JSON object")
        return {key: value.get(key) for key in ("vendor", "model", "serial", "firmware")}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, UnicodeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def inspect_network_targets(targets: list[NetworkTarget]) -> list[dict[str, Any]]:
    async def inspect(target: NetworkTarget) -> dict[str, Any]:
        item: dict[str, Any] = {
            "source": "network-target", "id": target.id,
            "address": target.url, "protocol": target.protocol,
            "probe": "configured-http-get" if target.probe else "not_probed",
            "control_status": "unconfigured",
            "next_step": "Use a reviewed, identity-pinned driver; discovery grants no control.",
        }
        if not target.probe:
            item["reason"] = "operator did not enable identity probing"
            return item
        if not target.expected_identity.get("serial"):
            item["reason"] = "active probing requires an exact expected serial identity"
            return item
        observed = await asyncio.to_thread(_probe, target)
        item["identity"] = observed
        if "error" in observed:
            item["reason"] = observed["error"]
        elif all(observed.get(k) == v for k, v in target.expected_identity.items()):
            item["control_status"] = "identity_verified"
            item["reason"] = "identity matches; a reviewed device driver is still required"
        else:
            item["reason"] = "identity mismatch; do not activate"
        return item

    return list(await asyncio.gather(*(inspect(target) for target in targets)))

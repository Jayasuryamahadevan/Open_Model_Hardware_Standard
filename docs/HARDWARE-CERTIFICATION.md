# Hardware control hardening and verification

This page describes the new HCP control modes and the actual certification
boundary. It does **not** claim untested physical devices are certified.

## Device discovery

`labbench analyze -c lab.yaml` and the MCP `hardware_analyze` tool report:

- Linux USB sysfs, with serial and VISA resource inventories where installed.
- macOS USB descriptors from `system_profiler SPUSBDataType -json`.
- Windows Plug-and-Play devices from a fixed PowerShell read-only query.
- SCPI `*IDN?` queries **only** to the targets configured by an operator.
- Configured control paths for online, identity-verified drivers.

Unrecognized devices are inventoried but not turned into controls.
Host enumeration cannot reveal proprietary command grammars.

## Configurable REST/JSON driver

Use `http_api` only after reviewing the actual device programming manual and
validating commands on the exact model/firmware. The driver accepts no
agent-selected URLs or paths; every command is explicitly declared.

```yaml
name: camera-bench
devices:
  - id: light1
    driver: http_api
    kind: instrument
    expected_identity: {serial: "SN123"}
    settings:
      base_url: "http://127.0.0.1:30000"
      token_env: "HCP_LIGHT_TOKEN"
      identity_path: "/identity"
      identity: {serial: "SN123", model: "Controller-1"}
      command_timeout_s: 4
      read_timeout_s: 3
      estop_path: "/stop"
      features:
        - identifier: Illumination
          properties:
            - name: brightness
              path: /brightness
              schema: {name: brightness, type: integer}
          commands:
            - name: set_brightness
              path: /brightness
              method: POST
              hazard: benign
              reversibility: restorable
              simulatable: false
              parameters:
                - name: brightness
                  type: integer
                  constraint: {minimum: 0, maximum: 100}
safety:
  autonomy: 0
```

The example endpoint is illustrative and does not imply any vendor supports
those routes. The operator must supply a tested implementation. An HTTP stop
command is not a substitute for an independent, physically tested E-stop.

`http_api` refuses redirect following, credentials in URLs, unknown route
methods, unbounded responses, unclassified commands and identity mismatch.
Non-GET commands must not be classified `hazard: none`. Motion or higher
hazards require an explicit stop endpoint in addition to independent physical
interlocks. Timeouts are **not retried automatically**: physical state is
uncertain and the device moves to FAULT.

## Authenticated remote MCP

```bash
LABBENCH_TOKEN="$(your-secret-provider)" \
labbench serve -c /absolute/path/lab.yaml -t mcp-http \
  --host 127.0.0.1 --port 8765
```

HTTP endpoint: `http://127.0.0.1:8765/mcp`. Put a trusted HTTPS reverse
proxy in front before accessing this from outside the laptop. Keep this
process bound to loopback where possible. The reverse proxy must authenticate
users or pass a validated bearer token without forwarding untrusted actor
headers. Prefer configured named `credentials` with fixed actor identities
over the legacy shared token if human approvals are enabled. Do not expose
`/rpc` or `/methods` through the proxy.

The `mcp-http` transport implements the **2025-11-25 stateless session
profile** of MCP Streamable HTTP: one JSON-RPC POST per message, application/json
responses, 202 for notifications, GET and DELETE return 405. It does not offer
GET SSE, does not issue sessions, and does not advertise 2026-07-28 semantics.
An Origin header is rejected unless explicitly added using a repeated
`--mcp-allowed-origin https://trusted.example` flag. Browser requests
without a matching Origin are rejected; CLI clients ordinarily omit Origin.
Every MCP call requires a bearer identity even on loopback.

## Recovery rules

- Hardware read/write operations may have explicit per-device timeouts.
- A command timeout marks the device faulted. Retrying an uncertain physical
  action risks duplicate actuation and is forbidden without operator review.
- Agents cannot invoke `device.clear_fault`. Physical `device.connect` also
  requires a verified human actor. Local untrusted access is not equivalent to
  an independently authenticated human.
- The global E-stop remains a separate, unblocked operation. Drivers may only
  offer a best-effort software stop; operators must verify hardware interlocks.

## Certification test matrix

| Test | Automated simulated CI | Physical HIL required |
| --- | --- | --- |
| Windows/macOS device inventory parsing | Yes, mock OS records | Confirm on each host OS |
| Adapter identity mismatch prevents activation | Yes, mock HTTP | Test wrong/disconnected device |
| Input validation prevents out-of-range command | Yes | Verify vendor-specific safe envelope |
| MCP invalid bearer / bad Origin rejected | Yes | Verify TLS proxy, identities and logs |
| Command deadline enters fault state | Yes | Verify motor/power behavior on timeout |
| Cable or network removal during command | Fault injection possible | Operator-supervised, nonhazardous bench |
| E-stop interrupts actual energy/motion | No | Mandatory tested physical stop |
| Reconnect only after operator inspection | Yes | Test recovery SOP and rehoming |
| Audit log traceability | Existing tests | Associate evidence with device serial |

## Opt-in real-hardware preflight

On a dedicated, operator-supervised machine with the actual device attached:

```bash
export HCP_HIL_CONFIG="/absolute/path/to/reviewed-lab.yaml"
export HCP_HIL_DEVICE_ID="physical_device_1"
export HCP_HIL_OPERATOR_ACK="READ_ONLY_ON_PHYSICAL_HARDWARE"
uv run pytest -q tests/hil/test_read_only_preflight.py
```

This executes read-only identity and telemetry probes on one selected device,
without an actuator command. It refuses simulated devices and requires a
serial-pinned configuration. Manual network disconnect, power-loss, timeout,
and hardware E-stop certification are separate operator-controlled tests;
never automate power removal or dangerous motions in public CI.

HIL is also available as an intentionally manual GitHub Actions workflow on
a **dedicated self-hosted runner** with label `hcp-hil`, protected by the
`hardware-lab` environment and an explicit operator acknowledgement.
It will not run on normal pull requests. Completing this preflight does not
constitute certification of motion or hazardous material handling.

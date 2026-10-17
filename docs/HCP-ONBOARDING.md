# Hardware onboarding and MCP

LabBench is an open HCP gateway implementation. It is **not** Anthropic's Model
Hardware Standard (MHS), which was announced as a research preview. It uses MCP
as the agent-facing protocol and drivers as adapters to each hardware protocol.

## Try the robotic arm flow on a laptop

From the repository root:

```bash
uv sync --extra dev
uv run labbench serve -c configs/simulated-arm.yaml -t mcp
```

Before connecting an agent, inspect the host without asking it to actuate anything:

```bash
uv run labbench analyze -c configs/simulated-arm.yaml
```

This outputs a JSON report containing the observed USB, serial, VISA and explicitly
configured SCPI addresses, the evidence supporting their identities, reviewed
activation bindings, active driver capabilities, JSON Schemas, and the exact
typed MCP tool names. An observed USB descriptor is not a control protocol.
Unrecognized devices remain inventory-only. The command does not send motion
or configuration commands, but configured instruments can be opened during
normal gateway startup and explicit SCPI targets receive the read-only
`*IDN?` identity query.

The last `serve` command speaks MCP JSON-RPC on stdin/stdout. It waits for an MCP client
to launch it; running it at an ordinary terminal is not useful. Configure a
stdio MCP client with an absolute path to the installed `labbench` executable
and absolute paths for the config and data directory. For example, in an MCP
client's `mcpServers` configuration:

```json
{
  "mcpServers": {
    "hcp": {
      "command": "/absolute/path/to/Hardware-context-protocol/.venv/bin/labbench",
      "args": ["--data-dir", "/absolute/path/to/hcp-data", "serve", "-c",
               "/absolute/path/to/Hardware-context-protocol/configs/simulated-arm.yaml",
               "-t", "mcp"]
    }
  }
}
```

Ask the client to discover the arm and make it wave hello. The intended tool
sequence is `hardware_analyze`, `device_describe`, `device_manual`,
`device_simulate`, `device_invoke` with `home`, `device_simulate` with `wave`,
`device_invoke` with `wave`, and `device_read`. The arm is simulated; no motor
will move. The core MCP tool catalog stays intentionally small. Use `--mcp-profile full` only\nwhen you need experiment, campaign, memory or advanced administrative tools.\nThe same stable core tool names work for every configured instrument.
`tools/list` includes JSON schemas and safety hints; `resources/list` exposes
live device capability descriptions at `hcp://device/{id}`.

## Connect a real instrument

1. Install the driver extra needed for its protocol, if any. SCPI over TCP has
   no extra dependency; USBTMC/GPIB SCPI needs `labbench[scpi]`. Cross-platform
   serial enumeration needs `labbench[discovery]`.
2. Add an explicit SCPI address to `scpi_targets` and restart the MCP server.
   `hardware_analyze` sends only `*IDN?` to that address. USB descriptors,
   serial ports and VISA resource names are inventoried without opening them.
3. Inspect the returned manufacturer, model, serial and firmware, then match
   the device to a driver and a reviewed command profile. A product ID or
   `*IDN?` reply does not specify the safe range of motion, electrical limits,
   or command semantics.
4. Add a device config with its driver, protocol settings, safety policy and
   operator-approved manual. Restart. Use `device_describe` to inspect the
   commands the driver actually exposes, then `device_simulate` before an
   action. Actions go through `device_invoke` and the existing approval and
   audit flow.

Example for a known SCPI instrument:

```yaml
name: my-bench
scpi_targets:
  - 192.0.2.10:5025
devices:
  - id: meter1
    driver: scpi
    kind: multimeter
    display_name: Bench meter
    settings:
      address: 192.0.2.10:5025
      profile: dmm
    expected_identity:
      model: DMM-Model-From-IDN
      serial: Exact-Serial-From-IDN
    manuals:
      - /absolute/path/to/meter-programming-manual.pdf
safety:
  autonomy: 0
```

Manual sources can be local files or operator-configured HTTPS URLs. Local
paths in YAML resolve relative to that YAML file. `device_manuals` lists them;
`device_manual` caches a copy, reports its SHA-256 digest, and returns up to
20,000 characters of text. Install `labbench[manuals]` for PDF text extraction.
The server does not search the web for a manual or trust arbitrary URLs from an
agent. Verify that the manual matches the exact hardware and firmware.

## Add another kind of robot or instrument

Write a `Device` driver and publish it in the `labbench.drivers` entry-point
group. On connect, read the device's identity and firmware. Define `Feature`,
`Property` and `Command` objects with units, bounds, hazard, reversibility and
preconditions. Implement `_read`, `_invoke`, `_simulate`, and `_estop`; an
honest simulation may report `fidelity: none`. Register the driver and add a
reviewed device config. The MCP tool surface stays fixed as drivers are added.

This is the necessary boundary for “any hardware”: HCP can discover many
connections and speak to supported protocols, but an unknown proprietary
protocol cannot be safely inferred from a cable or a PDF alone. New device
families need a driver, and each installation needs validated limits and an
effective physical emergency stop.

## Claude Desktop and OpenCode (local MCP)

Install the project into a virtual environment first. The executable and
configuration paths should be absolute; replace the sample paths with the
real checkout path. On Windows, use `.venv\\Scripts\\labbench.exe`.

Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hardware-context-protocol": {
      "command": "/absolute/path/to/Hardware-context-protocol/.venv/bin/labbench",
      "args": [
        "--data-dir", "/absolute/path/to/hcp-data",
        "serve", "-c",
        "/absolute/path/to/Hardware-context-protocol/configs/simulated-arm.yaml",
        "--transport", "mcp"
      ]
    }
  }
}
```

OpenCode `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "hardware-context-protocol": {
      "type": "local",
      "command": [
        "/absolute/path/to/Hardware-context-protocol/.venv/bin/labbench",
        "--data-dir", "/absolute/path/to/hcp-data",
        "serve", "-c",
        "/absolute/path/to/Hardware-context-protocol/configs/simulated-arm.yaml",
        "--transport", "mcp"
      ],
      "enabled": true
    }
  }
}
```

Restart the MCP client after editing its configuration. Ask the agent to call
`hardware_analyze` **first** and show the `analysis.endpoints`,
`analysis.devices` and `analysis.summary` sections. The client calls
`tools/list` to learn what it may invoke; after `hardware_activate`
connects a reviewed device, the gateway publishes
`notifications/tools/list_changed` and the newly generated
`hcp_<device>_<feature>_<command>_<hash>` tool names become available.

The alternative stable tool `device_invoke` works with an explicit device ID,
feature ID, command ID, and validated arguments. Both paths reach the same
safety policy, human approval checks, and audit log. A tool name being visible
does **not** grant permission to bypass those checks.

The `-t mcp` transport is MCP **stdio** and is intended for local clients.
`-t http` serves the project's own JSON-RPC gateway API; it is **not** an
MCP Streamable HTTP endpoint. Don't configure that native HTTP address as a
remote MCP URL. For a remote deployment, add and secure an MCP-compliant
transport separately rather than exposing the local stdio service.

## Control eligibility

| Connection | Analysis evidence | Conditions for control |
| --- | --- | --- |
| USB, HID, serial | OS descriptors, port names | Installed driver with verified command framing and safety model |
| VISA and SCPI | Resource address; `*IDN?` only for approved targets | Verified exact identity, reviewed profile and driver |
| W3C Web of Things | Operator-configured Thing Description | Validated adapter, declared affordances and site safety policy |
| OPC UA LADS and SiLA 2 | Driver-browsed features | Connected driver and approved site-specific hazard limits |
| Micro-Manager | Installed hardware configuration | Driver matches the physical instrument configuration |
| Opentrons | Vendor API and robot run state | Installed vendor adapter and appropriate robot permissions |

Do not infer motion ranges, voltage limits, chemical/biological controls or
emergency-stop behavior from USB IDs, product names, HTTP paths or LLM output.
Only remotely controllable hardware with a supported host interface is in
scope. Mechanical safety interlocks and a tested physical emergency stop
remain mandatory for real actuators.

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

The last command speaks MCP JSON-RPC on stdin/stdout. It waits for an MCP client
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
sequence is `hardware_discover`, `device_describe`, `device_manual`,
`device_simulate`, `device_invoke` with `home`, `device_simulate` with `wave`,
`device_invoke` with `wave`, and `device_read`. The arm is simulated; no motor
will move. The same stable MCP tools work for every configured instrument.
`tools/list` includes JSON schemas and safety hints; `resources/list` exposes
live device capability descriptions at `hcp://device/{id}`.

## Connect a real instrument

1. Install the driver extra needed for its protocol, if any. SCPI over TCP has
   no extra dependency; USBTMC/GPIB SCPI needs `labbench[scpi]`. Cross-platform
   serial enumeration needs `labbench[discovery]`.
2. Add an explicit SCPI address to `scpi_targets` and restart the MCP server.
   `hardware_discover` sends only `*IDN?` to that address. USB descriptors,
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

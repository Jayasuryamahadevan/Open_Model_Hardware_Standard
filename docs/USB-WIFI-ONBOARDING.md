# USB and Wi‑Fi Hardware Onboarding

HCP controls only hardware for which the host has a **real supported interface**:
USB CDC/serial, USBTMC/VISA/SCPI, network SCPI, W3C WoT, OPC UA LADS,
SiLA 2, approved REST/JSON, and vendor-specific installed driver packages.
Discovery never means a device can automatically be controlled.

**Safety boundary:** HCP is not a replacement for a physical emergency stop,
mechanical guards, electrical interlocks, safe power isolation, or operator
commissioning. Never enable motion, thermal, chemical, biological or radiation
hazards using an unreviewed command profile.

## 0. Install HCP and inspect the machine

```bash
git clone https://github.com/Jayasuryamahadevan/Hardware-context-protocol.git
cd Hardware-context-protocol
uv sync --extra dev --extra all --extra serial
uv run labbench doctor
uv run labbench analyze
uv run labbench harness
```

`labbench analyze` inventories local USB/serial/VISA/native hardware without
automatically issuing hardware commands. A config can additionally authorize
specific SCPI identity queries or specific Wi‑Fi endpoints (see below).
`labbench harness` runs **only simulated** protocol and safety smoke checks,
and refuses hardware configs.

## 1. USB CDC / serial (COM, ttyACM, ttyUSB)

1. Plug the device directly into the laptop or a suitable powered USB hub.
   Identify the exact model and use a manufacturer-approved data cable.
2. Enumerate ports. Linux: `lsusb`, `python -m serial.tools.list_ports -v`,
   `ls -l /dev/serial/by-id/`. macOS:
   `python -m serial.tools.list_ports -v`, `ls /dev/cu.*`.
   Windows PowerShell: `Get-PnpDevice -PresentOnly` or Device Manager →
   **Ports (COM & LPT)**; `python -m serial.tools.list_ports -v`.
   Install the vendor USB driver if the OS cannot enumerate the device.
3. Record the **USB vendor ID (VID), product ID (PID) and USB serial number**.
   Use a stable OS port mapping; if it changes, refresh the reviewed config.
4. Get the exact programming manual and establish: baud, parity (currently
   8N1), line ending, identity query, the expected identity reply, measurement
   queries, every proposed command, unit limits and a tested stop procedure.
   Do not guess these from the USB product string.
5. Copy `configs/usb-serial-reviewed.example.yaml` to a private location,
   replace all sample values, and keep `autonomy: 0` while commissioning.
   `serial_ascii` deliberately supports **ASCII-line command devices only**:
   it cannot drive binary USB protocols, raw HID, FTDI-specific modes or
   proprietary USB APIs. Those require appropriate dedicated driver plugins.
6. Read the profile and test on an inert/isolated bench. Then run
   `labbench analyze -c /absolute/path/reviewed.yaml` and configure your
   Claude/OpenCode MCP client to launch HCP with `-t mcp`. The client must
   first call `hardware_analyze`, inspect the exact typed tools, and simulate
   supported operations before attempting an approved action.

On Linux the user may need device-group access (e.g. `dialout` or `uucp`);
use standard udev permissions and reconnect your session. Do **not** run the
MCP server as root or grant world-writable access to serial devices.
A serial port being present does not prove the attached actuator is safe.

### USBTMC and USB instruments

Not all USB instruments enumerate as COM/tty. Oscilloscopes and multimeters
often expose VISA resources such as `USB0::...::INSTR`. Use the existing
`scpi` driver, install `labbench[scpi]` and the vendor's approved VISA
backend, and configure an exact resource, `*IDN?` binding and reviewed
profile. Serial ASCII profiles are **not** interchangeable with USBTMC
instruments. USB HID/webcams/storage are inventory-only until a supported
vendor/class adapter is installed.

## 2. Wi‑Fi and Ethernet hardware

1. Put the laptop and device on an authorized network/VLAN. Use the device's
   pairing procedure or vendor application to provision Wi‑Fi credentials.
   Never put Wi‑Fi passwords, API tokens, or device secrets in agent prompts.
2. Give the device a stable DHCP reservation or static local IP. Verify the
   laptop can reach it. Firewall access should be restricted to the HCP host
   and the minimum vendor-documented port(s); **do not port-forward devices
   directly to the Internet**.
3. Identify its actual control protocol: HTTP API, WoT Thing Description,
   SCPI TCP (often 5025), OPC UA, SiLA 2, or a vendor SDK. HCP cannot
   fabricate APIs from an IP address. Obtain an operator-reviewed manual and
   precise identity including device serial/firmware.
4. Copy `configs/wifi-network-targets.example.yaml` to your private config.
   Add an explicit `network_targets` entry. Targets accept only a private,
   loopback or link-local IP, or a `.local` hostname. No subnet scans or
   public Internet discovery are performed.
5. With `probe: false`, `hardware_analyze` lists the configured address but
   sends **no** packet to it. After verifying the identity endpoint is
   genuinely read-only, set `probe: true` and pin
   `expected_identity.serial`. HCP performs at most one bounded HTTP GET
   identity read per analysis; redirects and proxies are disabled.
6. Matching a Wi‑Fi identity remains **inventory only**. Separately configure
   and test an identity-pinned `http_api`, `wot`, `scpi`, `opcua_lads` or
   `sila2` driver. Live MCP tools appear only for connected drivers and
   continue through the safety kernel.
7. If the network changes, revalidate device identity and stop behavior.
   A network timeout may leave a physical action partially completed.
   Stop, inspect, and recover with an authorized operator before retrying.

A TLS endpoint with a trusted certificate is preferred. HCP's development
HTTP endpoint should bind only to `127.0.0.1`; terminate HTTPS using a
trusted reverse proxy for **remote MCP clients**, with fixed authenticated
client identities and strict network segmentation. A Wi‑Fi device control
port and an MCP server port are **different trust boundaries**.

## 3. Connect Claude Desktop or OpenCode over MCP

### Claude Desktop (stdio)

Add to `claude_desktop_config.json`, replacing paths:

```json
{
  "mcpServers": {
    "hcp": {
      "command": "/absolute/path/Hardware-context-protocol/.venv/bin/labbench",
      "args": [
        "serve", "-c", "/absolute/path/reviewed.yaml", "-t", "mcp"
      ]
    }
  }
}
```

### OpenCode (stdio)

Add to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "hcp": {
      "type": "local",
      "command": [
        "/absolute/path/Hardware-context-protocol/.venv/bin/labbench",
        "serve", "-c", "/absolute/path/reviewed.yaml", "-t", "mcp"
      ],
      "enabled": true
    }
  }
}
```

For remote deployment use `labbench serve -t mcp-http` with a named
bearer credential and a trusted HTTPS reverse proxy. The `/mcp` route
supports modern `2026-07-28` stateless core JSON calls alongside legacy
2025 handshakes. It does **not** yet implement the optional Tasks extension,
MRTR elicitation/sampling, `subscriptions/listen` streaming, or OAuth server
authorization. Avoid representing those as supported. For untrusted Internet
clients, require a separate security review and managed authorization.

## 4. Repeatable test harness and physical certification

```bash
uv run labbench harness
uv run pytest -q tests/test_modern_mcp_usb_wifi.py
uv run pytest -q tests/test_control_hardening.py
uv run pytest -q
```

The harness exercises modern discovery, schema enumeration, read-only
analysis, simulated capability descriptions and legacy initialization;
**no real device commands** execute. Automated tests use fake serial ports,
fake HTTP responses and simulated drivers.

Physical testing remains explicitly opt-in. See
[HARDWARE-CERTIFICATION.md](HARDWARE-CERTIFICATION.md) for the exact
serial-pinned HIL preflight and mandatory operator-supervised disconnect,
fault recovery and physical emergency-stop tests. A passing CI run does
not certify a physical product.

## 5. Add a vendor driver the right way

For a device with a proprietary binary protocol, native HID API or
vendor SDK, write a separate `Device` adapter. It must implement
`_features`, `_read`, `_invoke`, `_connect`, `_disconnect` and
`_estop`; the driver should verify model, serial and firmware on connect,
expose typed physical-unit constraints and hazards, and provide an honest
`simulate` fidelity indicator. Register it under the
`labbench.drivers` Python entry-point group. HCP projects verified
capabilities to MCP without changing the agent-facing safety flow.

Use the certification matrix above to test a simulated device model,
bad-identity refusal, malformed frames, timeouts, unplug events, partial
actions and safe recovery before marking a real driver production-ready.

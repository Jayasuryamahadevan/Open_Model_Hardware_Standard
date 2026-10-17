# HCP Architecture: Small Core, Verified Drivers

The goal is not to support every USB or Wi-Fi device through generated
commands. The goal is **a trustworthy contract** from an agent's intent to a
known physical capability.

## The one execution path

```text
Claude / OpenCode / MCP client
          |
          v
MCP stdio or authenticated MCP HTTP
          |
          v
Gateway router -> safety policy -> approval/ledger
          |
          v
DeviceManager (verified identity + quarantine)
          |
          v
Device (validated parameters + exclusive command lock + state checks)
          |
          v
Reviewed driver: serial ASCII / SCPI / WoT / REST / vendor adapter
          |
          v
Real instrument + independent physical safety interlock
```

No driver is allowed to expose arbitrary AI-chosen network URLs, serial
frames, or USB identifiers as executable commands. Drivers must define
features, parameters, units, operating constraints, hazard classes,
reversibility, and identity checks.

## Default MCP profile

The `core` profile is intentionally small: hardware analysis and binding,
device description, read, simulate, invoke, job completion/cancellation,
and E-stop. Advanced memory, campaigns, experiment planning, and provenance
queries live in `full`, enabled explicitly with
`labbench serve -t mcp --mcp-profile full` or
`labbench serve -t mcp-http --mcp-profile full`.

The native JSON-RPC gateway retains its existing methods for automation and
backward compatibility. A legacy `hardware_discover` MCP tool call continues
to work, but the default catalog advertises only `hardware_analyze`.
Reviewed device-specific typed tools remain projected into MCP dynamically.

## Physical-state invariants

1. **Command execution requires an operational device** even for
   `hazard: none` queries. Parameter validation and policy checks are
   not a substitute for state gating.
2. **Exclusive commands recheck state after obtaining the lock.** A queued
   command cannot begin if an earlier command disconnected, timed out, or
   faulted the device.
3. **A fault cannot be cleared into IDLE.** Clearing disconnects the
   instrument, sets OFFLINE, and requires a separately authorized reconnect.
4. **Manual reconnect uses the same identity-verification path as startup.**
   Device IDs, expected serials, and OS descriptors supplied in YAML are
   claims—not proof of hardware identity. The driver must independently
   measure/verify the identity at connection time.
5. **Quarantine persists across disconnection.** An authenticated human
   operator can record inspection and clear the fault, but identity mismatch
   immediately quarantines the next attempted connection again.
6. **Physical emergency stops are independent.** HCP's software stop is
   best-effort and never replaces an electrical or mechanical interlock.

## Scope boundary

A USB descriptor identifies a bus connection, not an executable protocol.
A network endpoint proves reachability, not command semantics. Only
reviewed protocol drivers bridge this gap. Use
[USB and Wi-Fi onboarding](USB-WIFI-ONBOARDING.md) for commands and
[physical certification](HARDWARE-CERTIFICATION.md) for HIL requirements.

## Test strategy

- **Unit tests:** parameter and lifecycle invariants, corrupted identity,
  cancellations, queuing, and timeout failure injection.
- **Harness:** deterministic simulated-only MCP client requests with host
  discovery patched out; `uv run labbench harness`.
- **CI:** lint, the full test suite, and the harness.
- **Physical HIL:** opt-in, serial-pinned, operator-supervised; verify actual
  power/motion stop, unplugged connections and required recovery SOPs.
  Public CI cannot certify physical actuation.

## Non-goals

No auto-generated commands from unknown hardware; no default subnet scan;
no autonomous re-connection after uncertain motion; no production certification
claim based only on mocks; no additional orchestration framework until a
measured, reproducible engineering need warrants it.

# OpenTeleop Security & Safety Design

## 1. Threat Model

Remote teleoperation exposes a robot to a network. The threat model covers
four classes:

| Threat | Example | Mitigation |
|--------|---------|------------|
| **Network-level** | man-in-the-middle, replay | DTLS 1.2+ (WebRTC), TLS (signaling), nonce anti-replay |
| **Command injection** | malformed / unknown commands | strict whitelist + schema validation (pydantic) |
| **Link failure** | operator disconnect, packet flood | L1 heartbeat deadman, L2 dual-path e-stop |
| **Local fault** | joint overspeed, torque spike | L3 independent hardware safety monitor |

## 2. Four-Level Safety

### L1 — Heartbeat deadman (`HeartbeatMonitor`)

* Operator sends a `ping` state command at `heartbeat_hz` (default 10 Hz).
* If the robot sees no heartbeat for `heartbeat_timeout_ms` (default 500 ms):
  1. velocity ramps to zero over `ramp_down_ms` (200 ms) — smooth, not a stop;
  2. nav goals are cancelled;
  3. recovery **requires an explicit operator confirmation** — never
     auto-resumes, to avoid surprising motion after a blind reconnect.

### L2 — Dual-path e-stop (`DualPathEstop`)

* The e-stop command travels over **two independent paths**:
  1. the reliable state channel;
  2. an optional WebSocket connection.
* The robot reacts to whichever arrives first. Both are **idempotent** — a
  repeated e-stop is a no-op, so out-of-order delivery cannot re-energize.
* Full e-stop performs a hardware stop (motors de-energized / brakes engaged),
  not a ramp.

### L3 — Independent local safety monitor (`SafetyMonitor`)

* Runs in its **own thread**, reading hardware state directly (not through the
  control stack or the network).
* Monitors: joint velocity, joint torque, end-effector force, workspace
  boundaries, collision (current spike).
* On violation: hardware e-stop **bypassing the software stack**, plus a
  black-box log of the triggering state.
* This layer survives: operator link loss, transport failure, and control-loop
  crashes.

### L4 — Video-freeze gate (`VideoGuard`)

* If no video frame arrives within `video_freeze_timeout_ms` (default 300 ms),
  new command input from the operator is **blocked**.
* Prevents the operator from commanding the robot based on a stale image.
* Recovery requires the operator to explicitly confirm that video has resumed.

## 3. Command Authentication & Integrity

* **Whitelist**: every `state_reliable` type must be in the robot-side
  whitelist; unknown types are rejected and logged.
* **Schema validation**: payloads are validated (pydantic models in production
  builds) before execution.
* **Nonce anti-replay**: each command carries a UUID nonce; acks echo it, so a
  forged or replayed command cannot masquerade as an ack.
* **Rate limiting** (recommended): enforce a maximum command rate on the robot
  side to blunt flood attacks.

## 4. Network Security

* WebRTC data and media are encrypted with **DTLS-SRTP**; no plaintext payloads
  traverse the wire.
* Signaling must run over **TLS** (HTTPS / WSS).
* For internet deployments, prefer a **VPN or mutual-TLS** between operator and
  robot when operating outside a controlled fleet network.
* STUN/TURN servers should be fleet-controlled; avoid third-party TURN relays
  for sensitive payloads.

## 5. Deployment Safety Checklist

- [ ] Hardware e-stop physically wired (mandatory; software e-stop is not a substitute).
- [ ] Independent safety monitor enabled and its limits tuned to the robot's spec.
- [ ] Workspace boundary configured.
- [ ] Heartbeat + ramp-down verified by a deliberate cable-pull test.
- [ ] Video-freeze gate tested by blocking the camera feed.
- [ ] Operator has a visible, low-latency e-stop button.
- [ ] Black-box logging enabled for post-incident analysis.

## 6. References

* ISO 10218 (industrial robots — safety requirements)
* ISO/TS 15066 (collaborative robots)
* dimos deadman design: `dimensionalOS/dimos` Hosted Teleop
* BEAVR safety notes: `ARCLab-MIT/beavr-bot`

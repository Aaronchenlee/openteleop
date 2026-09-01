# OpenTeleop Wire Protocol

## 1. Control Channel (`cmd_unreliable`)

Fixed-layout binary (no JSON — minimizes serialization overhead at 50–100 Hz).

```
Byte  Size  Field            Description
0     8     timestamp_us     sender monotonic clock (µs)
8     1     seq              sequence number (0–255, wraps; loss stats)
9     1     mode             0=EE pose, 1=joint, 2=velocity
10    1     side             0=right, 1=left, 2=both
11    1     n_dof            number of float32 values that follow
12    4*n   action_data      float32 array (pose or joints)
```

Encoding is implemented in `openteleop.channels.command.CommandChannel`.
The header keeps every packet self-describing, so a receiver can validate
length and drop malformed frames without state.

## 2. Reliable Command Channel (`state_reliable`)

JSON envelope with a **nonce** for idempotent ack tracking:

```json
{
  "type": "set_mode | config | reset | home | e_stop | skill_trigger",
  "nonce": "uuid-v4",
  "payload": {}
}
```

Every command is whitelisted on the robot side (`CommandRouter._allowed`).
Unknown types are rejected with `{"ok": false, "error": "not allowed"}` — no
silent execution.

### Ack (returned over `state_reliable_back`)

```json
{
  "type": "cmd_ack",
  "nonce": "<echoed nonce>",
  "ok": true,
  "mode": "teleop"
}
```

The operator's `StateChannel.send()` awaits the ack with a 2 s timeout.

### Command dictionary

| type | payload | behavior |
|------|---------|----------|
| `e_stop` | `{"level": "full"\|"damp"}` | full → hardware stop; damp → 200 ms ramp |
| `set_mode` | `{"mode": "idle"\|"teleop"\|"autonomy"}` | switch control mode, reset limiter |
| `reset` | `{}` | reset baseline, return to idle |
| `home` | `{}` | send zero action to robot |
| `config` | `{"speed_scale": 0.5, "force_limit": 5.0}` | dynamic parameter tuning |
| `skill_trigger` | `{"skill_id": "...", "params": {}}` | trigger atomic skill (shared autonomy) |

## 3. Telemetry Channel (`state_reliable_back`)

Periodic JSON telemetry (default 5 Hz) with transport stats embedded:

```json
{
  "ts": 1717171717000000,
  "stats": {
    "cmd_unreliable": {"rate_hz": 98.3, "latency_ms": 23.5,
                       "p95_latency_ms": 30.1, "jitter_ms": 2.1, "loss_pct": 0.2}
  },
  "robot_state": {"joint_positions": [], "joint_velocities": [], "ee_force_mag": 2.3},
  "mode": "teleop",
  "estopped": false,
  "heartbeat_alive": true
}
```

## 4. Tactile Channel (`tactile_unreliable`)

Binary, timestamped, no-retransmit:

```
Byte  Size  Field
0     8     timestamp_us
8     1     n_fingers
9     4*n   force_per_finger  (float32, N)
```

Consumed by the operator's `SyncBuffer` for display and by the (optional)
haptic feedback device.

## 5. Video Channel

Media stream (RTP in WebRTC mode, pickled frames in ZMQ mode for demos).
Production deployments push hardware-encoded frames (NVENC / VA-API) with
adaptive bitrate — see [deployment.md](deployment.md) for the rate ladder.

## 6. QoS Guarantees Summary

| Channel | Ordered | Retransmit | Worst-case effect of loss |
|---------|---------|------------|---------------------------|
| cmd_unreliable | no | no | robot holds last command (ZOH) until next sample |
| state_reliable | yes | yes | ack timeout → operator retries or aborts |
| state_reliable_back | yes | yes | operator shows stale telemetry |
| video | — | no | frame drop → brief visual glitch |
| tactile_unreliable | no | no | one force sample skipped |
| audio | — | no | brief audio dropout |

## 7. QoS Negotiation & Authentication (adapter layer)

Session bring-up rides the reliable `state_reliable` channel (REQ/REP). Three
message types are defined on top of the transport envelope:

### 7.1 Handshake (cloud → edge)

```json
{
  "type": "openteleop.handshake",
  "client_id": "cloud-01",
  "ts": 1754000000.123,
  "sig": "hex-of-hmac-sha256(secret, client_id|ts)"
}
```

Edge verifies the signature (TTL 60 s) and replies:

```json
{
  "ok": true,
  "cred": {
    "session_id": "uuid",
    "client_id": "cloud-01",
    "allowed_topics": ["arm/cmd", "custom/pc"],
    "upstream_quota_bps": 2000000,
    "downstream_quota_bps": 0,
    "issued_at": 1754000000.123
  }
}
```

Failure returns `{"ok": false, "code": "auth_failed"}`.

### 7.2 Negotiation (cloud → edge)

```json
{
  "type": "openteleop.negotiate",
  "declarations": {
    "custom/pc": {
      "rate_hz": 20, "max_latency_ms": 0, "max_jitter_ms": 0,
      "upstream_bps": 1000000, "downstream_bps": 0,
      "packet_size_bytes": 2000, "reliability": "best_effort",
      "ordering": true, "max_loss_pct": 0.0, "priority": 0
    }
  }
}
```

Edge merges stricter-wins (only topics declared on both sides survive), checks
the aggregate bandwidth budget, and replies with the agreed table plus its
channel binding table:

```json
{
  "ok": true,
  "agreed": { "<topic>": { ...qos } },
  "bindings": {
    "custom/pc": {"channel": "dyn_custom_pc", "port": 6100, "owner": "robot", "qos": {...}}
  }
}
```

Over-budget returns `{"ok": false, "code": "bandwidth_exceeded", "error": "..."}`.

### 7.3 Dynamic channels

Topics that don't unambiguously map to the fixed six get `dyn_<topic>` channels
from port 6100+. The binding's `owner` (who binds, the other side connects) is
chosen by data direction. Cloud adopts the edge's table after negotiation and
joins dynamic subscriptions at runtime (`transport.add_subscription`).

### 7.4 Monitoring

`QoSDegrader` samples per-topic metrics and, on violation of the effective
contract, walks the ladder reliability → rate → bitrate → packet size, then
recovers stepwise. See [adapter.md](adapter.md).

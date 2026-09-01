# OpenTeleop Architecture

## 1. Design Principles

OpenTeleop distills five engineering principles proven across the reference
research systems:

1. **Channel separation** — different data types get purpose-tuned reliability.
   A stale control command is worse than no command; a stale mode switch must
   never be dropped.
2. **Dual-mode transport** — ZMQ for the lowest latency on LAN, WebRTC for NAT
   traversal on the internet. The business layer only sees `BaseTransport`.
3. **Timestamp-driven synchronization** — every outbound sample carries the
   sender's monotonic clock. Receivers align by timestamp, never by arrival
   order.
4. **Safety independent of business** — heartbeat, e-stop, and local monitor
   survive business-channel failures.
5. **Composable sessions** — operator and robot are two symmetric sessions that
   can run in separate processes, separate hosts, or even on one host for
   testing.

## 2. System Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Operator Station                             │
│  ┌──────────┐  ┌──────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │ Input    │→ │Retargeter│→ │CommandChannel│→│ BaseTransport    │  │
│  │ Adapter  │  │ (vector) │  │(rate+vel lim)│ │ (ZMQ / WebRTC)  │  │
│  └──────────┘  └──────────┘  └─────────────┘  └───────┬─────────┘  │
│                                                        │           │
│  ┌────────────┐  ┌────────────┐  ┌───────────────────┐│           │
│  │ UI / HUD   │← │ SyncBuffer │← │ video+state+tactile│←──────────┘
│  └────────────┘  └────────────┘  └───────────────────┘              │
└────────────────────────────────┬────────────────────────────────────┘
                                 │  LAN: ZMQ PUB/SUB
                                 │  WAN: WebRTC (SFU / P2P)
                                 ▼
┌────────────────────────────────┴────────────────────────────────────┐
│                         Robot Station                                │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  BaseTransport  →  CommandRouter  →  SharedAutonomyLayer     │   │
│  │  (cmd,state)        (whitelist,    (force residual,         │   │
│  │                      vel-limit)     on-robot, not on net)    │   │
│  │                          │                                   │   │
│  │                          ▼                                   │   │
│  │                     RobotController (vendor driver)          │   │
│  │   camera/tactile → transport → operator                      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│   Safety (independent): HeartbeatMonitor · DualPathEstop ·          │
│                          SafetyMonitor · VideoGuard                 │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. Six-Channel Protocol

| # | Channel | Direction | Reliability | Rate | Payload |
|---|---------|-----------|-------------|------|---------|
| 1 | `cmd_unreliable` | op → robot | unordered, no-retransmit | 50–100 Hz | binary joint/EE action |
| 2 | `state_reliable` | op → robot | ordered, reliable | event | JSON + nonce + ack |
| 3 | `state_reliable_back` | robot → op | ordered, reliable | 3–10 Hz | telemetry + acks |
| 4 | `video` | robot → op | media (RTP) | 30 fps | hardware-encoded frames |
| 5 | `tactile_unreliable` | robot → op | unordered, no-retransmit | 30–100 Hz | binary force data |
| 6 | `audio` (optional) | bidirectional | media | 16 kHz | Opus |

### Why is the control channel unreliable?

Teleoperation is a **latest-wins** control problem: the operator's current
command supersedes any earlier one. Retransmitting a stale command (which
arrives after newer data) injects contradictory targets into the robot.
`ordered=False, maxRetransmits=0` on the SCTP DataChannel guarantees
**freshness over completeness**. The robot's `VelocityLimiter` then provides
first-order-hold smoothing so packet loss produces a small delay, never a jump.

## 4. Transport Modes

### LocalZMQTransport (LAN)

* One background thread per `(host, port)` publisher (BEAVR
  `ZMQPublisherManager` pattern).
* PUB/SUB with HWM: when the queue is full, the **oldest** pending message is
  dropped (stale commands are discarded first).
* Reliable commands use REQ/REP with a 2 s timeout + ack.
* Measured: <15 ms one-way @ 90 Hz on loopback, <1 ms jitter.

### RemoteWebRTCTransport (WAN)

* One `RTCPeerConnection`, six logical channels multiplexed (BUNDLE).
* STUN/TURN for NAT traversal; signaling over HTTPS/WebSocket only during
  connection setup.
* Media tracks carry video/audio; SCTP DataChannels carry commands/state.

### Factory (AUTO)

`TransportFactory.create()` probes the peer's command port; reachable → ZMQ,
else → WebRTC. Forced modes (`LOCAL` / `REMOTE`) are also supported.

## 5. Synchronization (SyncBuffer)

Video, state, and tactile travel different paths with different latency
(30–80 ms vs 10–30 ms). Using "latest frame + latest state" shows a 30–50 ms
misalignment. `SyncBuffer`:

1. Stamps every outbound sample with the sender's monotonic clock.
2. On the operator side, buffers recent samples per channel.
3. For each video frame at `t_f`, finds the nearest state and tactile within
   `search_range = tolerance_ticks / fps` (default 50 ms @ 30 fps).
4. Emits an aligned `Observation`, or drops with a counter if nothing is within
   range (never blocks the control loop).

## 6. Safety Architecture

| Level | Mechanism | Scope | Failsafe behavior |
|-------|-----------|-------|-------------------|
| L1 | `HeartbeatMonitor` | operator link loss | velocity ramp to zero over 200 ms |
| L2 | `DualPathEstop` | e-stop command | DataChannel + WebSocket, first-wins, idempotent |
| L3 | `SafetyMonitor` | hardware limits | independent thread, direct HW read → HW e-stop |
| L4 | `VideoGuard` | stale video | block new command input until operator confirms |

## 7. Robot / Operator Sessions

* `RobotSession` — owns the robot, the router, the shared-autonomy layer, the
  telemetry publisher, and the full safety stack. Wire the vendor driver by
  implementing `RobotController`.
* `OperatorSession` — owns the input adapter, retargeter, command channel, the
  SyncBuffer, and the video-freeze gate. Wire a real input device by
  implementing `InputAdapter` (VR headset, data glove, etc.).
* Both sessions expose `start()/stop()`, `send_command()`, and `get_stats()`
  for integration into larger applications.

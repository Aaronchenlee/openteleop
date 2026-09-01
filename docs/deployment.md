# OpenTeleop Deployment Guide

## 1. Deployment Topologies

### Topology A — Same host (demo / test)

```
operator session ── loopback ZMQ ── robot session (DummyRobot)
```

Run `openteleop demo --seconds 5`. No network, no hardware needed.

### Topology B — LAN (production minimum)

```
Operator PC ──── gigabit LAN / WiFi6 ──── Robot IPC (robot + controller)
```

* Transport: ZMQ (`--local`), peer IP reachable → <15 ms one-way.
* Suitable for in-factory teleop, data collection for imitation learning.

### Topology C — Internet (remote operation)

```
Operator PC ── Internet ── SFU/TURN ── Internet ── Robot (with public egress)
```

* Transport: WebRTC (AUTO picks it when ZMQ probe fails).
* Requires a signaling server (HTTPS/WebSocket) and STUN/TURN.
* Suitable for cross-site / cross-city operation.

## 2. Network Requirements

| Metric | Minimum | Recommended | Notes |
|--------|---------|-------------|-------|
| One-way latency | < 60 ms | < 40 ms | >100 ms makes fine manipulation hard |
| Jitter | < 30 ms | < 20 ms | jitter hurts more than average latency |
| Packet loss | < 2% | < 1% | control channel tolerates; video degrades |
| Uplink bandwidth | 2 Mbps | 10–30 Mbps | adaptive video 1–6 Mbps; control ~100 kbps |
| Network type | — | 5G slice / MPLS / MEC / LAN | avoid shared consumer WiFi for critical ops |

## 3. Video Rate Ladder (adaptive)

| Bandwidth | Resolution | FPS | Bitrate |
|-----------|-----------|-----|---------|
| > 20 Mbps | 1280×720 | 30 | 4–6 Mbps |
| 10–20 Mbps | 960×540 | 30 | 2–3 Mbps |
| 5–10 Mbps | 640×480 | 30 | 1–2 Mbps |
| 2–5 Mbps | 640×480 | 15 | 0.5–1 Mbps |

Prefer **hardware encoding** (NVENC / VA-API / VPU). CPU software encoding
adds 20–50 ms of glass-to-glass latency and consumes cores needed by control.

## 4. Configuration

Configuration is pydantic-validated (`openteleop.config.settings.TeleopConfig`).

```python
from openteleop.config.settings import TeleopConfig, TransportMode, VideoConfig

cfg = TeleopConfig.default_robot()
cfg.mode = TransportMode.LOCAL
cfg.ctrl_hz = 100
cfg.video = VideoConfig(codec="h264", width=1280, height=720, fps=30,
                        hardware_encode=True, adaptive_bitrate=True)
```

Or load from a JSON file:

```bash
openteleop robot --local --n-dof 6
# with: from_file('robot.json')
```

## 5. Integrating a Real Robot

Implement `RobotController`:

```python
from openteleop.robot.controller import RobotController
import numpy as np

class MyUR(RobotController):
    @property
    def n_dof(self): return 7
    def read_state(self): return {...}   # joints, vels, torques, ee force
    def send_action(self, action: np.ndarray): ...  # to your driver
    def emergency_stop(self): ...
    def ramp_down_to_zero(self, duration_s): ...
```

Then pass it to `RobotSession`:

```python
from openteleop.robot.session import RobotSession

session = RobotSession(cfg, robot=MyUR())
await session.start()
```

## 6. Integrating a Real Input Device

Implement `InputAdapter` (VR headset, data glove, ...):

```python
from openteleop.operator.input_adapter import InputAdapter

class MyVRGlove(InputAdapter):
    def read_action(self): ...   # fingertip positions/directions
    def has_new_sample(self): ...
```

Wire through the retargeter (identity or vector-level) and into
`OperatorSession`.

## 7. Performance Tuning

* Control rate: 50–100 Hz is typical; 100 Hz needs `ctrl_hz=100` and a real-time
  thread on the robot side.
* SyncBuffer `tolerance_ticks`: raise if video and state skew on bad links;
  lower for tighter alignment.
* Heartbeat timeout: keep ≥ 3× expected jitter to avoid false deadmans.
* VelocityLimiter `max_delta`: tune to the robot's real acceleration limit.

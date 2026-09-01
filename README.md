<div align="center">

# 🤖 OpenTeleop

**Production-grade, commercially-licensed open-source teleoperation framework for bimanual dexterous manipulation.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green)](pyproject.toml)
[![Status](https://img.shields.io/badge/Status-Beta-yellow)]()

</div>

OpenTeleop is a **commercially usable** (Apache-2.0) framework for remote-control of dexterous robot manipulators, distilled from the engineering patterns proven in leading research systems:

| Reference | What we take from it |
|---|---|
| **[BEAVR](https://github.com/ARCLab-MIT/beavr-bot)** (MIT) | ZMQ three-process architecture, drop-oldest HWM, slow-joiner handshake |
| **[dimos](https://github.com/dimensionalOS/dimos)** Hosted Teleop | Six-channel WebRTC layout, JSON+nonce+ack command protocol, 0.2s deadman safety |
| **[LiveKit Portal](https://github.com/livekit/portal)** | Timestamp-synchronized multimodal streaming (SyncBuffer) |
| **[DexTeleop-0](https://arxiv.org/abs/2606.23431)** (NTU/灵御) | Force-residual shared autonomy, six-layer system design |
| **[dex-retargeting](https://github.com/dexsuite/dex-retargeting)** | Vector-level motion retargeting (fingertip + direction matching) |

---

## ✨ Highlights

- **Dual-mode transport** — ZMQ for LAN (<15 ms one-way), WebRTC for internet (NAT traversal via STUN/TURN), auto-selected at runtime, same API for both.
- **Six-channel separation** — control (unreliable, no-retransmit), reliable JSON commands, telemetry, video, tactile, audio each with purpose-tuned QoS.
- **Four-level safety** — heartbeat deadman, dual-path e-stop, independent local safety monitor, video-freeze gate.
- **Time-synchronized multimodal feedback** — SyncBuffer aligns video/state/tactile by sender timestamps (30–50 ms skew eliminated).
- **Force-residual shared autonomy hook** — robot-side residual layer (DexTeleop-0 style) that never traverses the network.
- **Vendor-neutral robot interface** — bring your own UR/Sharpa/Unitree/Franka driver via `RobotController`.
- **QoS adapter layer** — edge/cloud symmetric API (`advertise` / `subscribe` / `request`) with 8-dimension QoS contracts (rate / latency / jitter / up-down bandwidth / packet size / reliability / ordering), HMAC handshake + topic ACL + bandwidth quota (network-side auth), stricter-wins negotiation, a dynamic channel pool beyond the fixed six, and runtime monitoring with automatic degradation.

---

## 🏗 Quick start

```bash
# 1. Create a virtualenv and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

# 2. Run a full operator+robot demo on one machine (loopback, no network)
openteleop demo --seconds 5

# 3. Or run two stations (local mode, two terminals)
#    Terminal A - robot:
openteleop robot --local --n-dof 6
#    Terminal B - operator:
openteleop operator --local --peer 127.0.0.1 --hz 50
```

---

## 📚 Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Overall architecture, module design, data flow |
| [docs/protocol.md](docs/protocol.md) | Wire protocol: six channels, message formats, QoS |
| [docs/security.md](docs/security.md) | Four-level safety design and threat model |
| [docs/deployment.md](docs/deployment.md) | Deployment topologies, network requirements, tuning |
| [docs/adapter.md](docs/adapter.md) | QoS adapter layer: API, negotiation, auth, degradation |

---

## 🧱 Repository layout

```
openteleop/
├── src/openteleop/
│   ├── transport/        # BaseTransport + ZMQ + WebRTC + factory
│   ├── channels/         # Command/State/Telemetry channel adapters
│   ├── sync/             # SyncBuffer (multimodal time alignment)
│   ├── safety/           # L1-L4 safety mechanisms
│   ├── robot/            # RobotController + CommandRouter + session
│   ├── operator/         # InputAdapter + OperatorSession
│   ├── retarget/         # Motion retargeting engine
│   ├── adapter/          # QoS adapter layer (edge/cloud symmetric API)
│   └── cli/              # openteleop command-line interface
├── docs/                 # Architecture / protocol / security / deployment
├── examples/             # Runnable examples
├── tests/                # Unit + integration tests
└── pyproject.toml        # Packaging (Apache-2.0)
```

---

## 🧪 Testing

```bash
pytest tests/ -v
```

Covers: transport round-trips (ZMQ), command wire format, velocity limiting, SyncBuffer alignment, heartbeat deadman, safety monitor limits, video-freeze gate, command router (teleop flow + whitelist + e-stop), full operator↔robot end-to-end loopback integration, and the QoS adapter layer (QoS validation/merge, dynamic channel allocation, HMAC auth + ACL + quota, bandwidth negotiation, degradation ladder, and edge↔cloud handshake+negotiation+dynamic-topic dataflow integration).

---

## 📄 License

**Apache License 2.0** — free for commercial use, modification, and redistribution. See [LICENSE](LICENSE).

> ⚠️ **Safety notice** — Teleoperation of physical robots can cause injury or property damage. Always deploy with a hardware e-stop, an independent safety monitor, and human supervision. OpenTeleop provides software safety layers but cannot substitute for compliant hardware safety design (ISO 10218 / ISO/TS 15066).

---

## 🗺 Roadmap

- [x] QoS adapter layer (edge/cloud symmetric API, negotiation, auth, degradation)
- [ ] WebRTC signaling server (HTTPS/WebSocket reference implementation)
- [ ] Hardware encoder integration (NVENC / VA-API) for the video channel
- [ ] Adaptive bitrate (ABR) on the video channel driven by the monitor
- [ ] Shared-autonomy residual QP solver (DexTeleop-0 style)
- [ ] Data recording for imitation-learning datasets (shared teleop + autonomy)
- [ ] Multi-operator room model (LiveKit Portal style)

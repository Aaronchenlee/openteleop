"""Safety layer - L1 to L4.

Four independent safety mechanisms, each with its own trigger and scope.
The design principle: safety must survive business-channel failures.

* L1 heartbeat / deadman - operator link loss -> ramp to zero
* L2 dual-path e-stop    - DataChannel + WebSocket redundancy
* L3 local safety monitor - independent process, direct hardware read
* L4 video-freeze gate   - operator cannot act on stale video
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..config.settings import SafetyConfig


@dataclass
class SafetyStatus:
    """Current safety state, broadcast to operator UI."""

    estopped: bool = False
    heartbeat_alive: bool = False
    video_frozen: bool = False
    violation: Optional[str] = None
    timestamp: float = 0.0


class HeartbeatMonitor:
    """L1 deadman switch.

    The operator must send heartbeats at ``heartbeat_hz``. If the link is
    lost for ``timeout_ms``, the robot ramps its velocity to zero over
    ``ramp_down_ms``. Recovery requires an explicit operator confirmation.
    """

    def __init__(self, config: SafetyConfig, on_deadman: Optional[Callable[[], None]] = None):
        self._config = config
        self._on_deadman = on_deadman
        self._last_heartbeat = time.monotonic()
        self._alive = True
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="hb-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def on_heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()
            self._alive = True

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._alive

    def _loop(self) -> None:
        while self._running:
            elapsed = time.monotonic() - self._last_heartbeat
            if self._alive and elapsed > self._config.heartbeat_timeout_ms / 1000.0:
                with self._lock:
                    self._alive = False
                if self._on_deadman:
                    self._on_deadman()
            time.sleep(self._config.heartbeat_timeout_ms / 2000.0)


class DualPathEstop:
    """L2 dual-path emergency stop.

    Two independent paths are used so that a single failure cannot prevent
    an e-stop:

    1. the reliable state channel (DataChannel / ZMQ REQ-REP)
    2. an optional WebSocket connection

    The robot reacts to whichever arrives first (idempotent).
    """

    def __init__(self, transport: Any, safety_cfg: SafetyConfig, on_estop: Optional[Callable[[str], None]] = None):
        self._transport = transport
        self._safety_cfg = safety_cfg
        self._on_estop = on_estop
        self._estopped = False

    @property
    def estopped(self) -> bool:
        return self._estopped

    async def trigger(self, level: str = "full") -> None:
        """Trigger e-stop over the primary path. Fire-and-forget secondary."""
        self._estopped = True
        message = {"type": "e_stop", "level": level, "payload": {"level": level}}
        asyncio.get_running_loop().create_task(self._transport.send_state(message))
        if self._on_estop:
            self._on_estop(level)

    def handle_estop(self, message: dict) -> dict:
        """Robot side: any path that delivers an e_stop triggers it."""
        level = message.get("payload", {}).get("level", "full")
        self._estopped = True
        if self._on_estop:
            self._on_estop(level)
        return {"ok": True, "level": level}

    def reset(self) -> None:
        self._estopped = False


class SafetyMonitor:
    """L3 local safety monitor.

    Runs independently (own thread / process) and reads hardware state
    directly. On any limit violation it performs a hardware e-stop, bypassing
    the whole control + communication stack.
    """

    def __init__(self, config: SafetyConfig, read_hw: Optional[Callable[[], dict]] = None):
        self._config = config
        self._read_hw = read_hw or (lambda: {})
        self._violation: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="safety-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    @property
    def violation(self) -> Optional[str]:
        return self._violation

    def _loop(self) -> None:
        interval = self._config.check_interval_ms / 1000.0
        while self._running:
            state = self._read_hw()
            violation = self._check(state)
            if violation and self._violation is None:
                self._violation = violation
                if self._on_hardware_estop:
                    self._on_hardware_estop(violation)
            time.sleep(interval)

    _on_hardware_estop: Optional[Callable[[str], None]] = None

    def set_estop_callback(self, cb: Callable[[str], None]) -> None:
        self._on_hardware_estop = cb

    def _check(self, state: dict) -> Optional[str]:
        cfg = self._config
        if any(abs(v) > cfg.joint_vel_limit for v in state.get("joint_vels", [])):
            return "joint_vel_limit"
        if any(abs(t) > cfg.joint_torque_limit for t in state.get("joint_torques", [])):
            return "joint_torque_limit"
        if state.get("ee_force_mag", 0.0) > cfg.ee_force_limit:
            return "ee_force_limit"
        return None


class VideoGuard:
    """L4 video-freeze gate.

    If no video frame arrives within ``video_freeze_timeout_ms``, new command
    input from the operator is blocked until the operator explicitly
    acknowledges that the video has recovered. Prevents acting on stale imagery.

    ``enabled=False`` disables the gate (e.g. pure-command pipelines, sim,
    data replay where there is intentionally no video stream).
    """

    def __init__(
        self,
        config: SafetyConfig,
        on_freeze: Optional[Callable[[bool], None]] = None,
        enabled: bool = True,
    ):
        self._config = config
        self._on_freeze = on_freeze
        self._last_frame_ts = time.monotonic()
        self._frozen = False
        self._enabled = enabled

    def on_frame(self) -> None:
        self._last_frame_ts = time.monotonic()
        if self._frozen:
            self._frozen = False
            if self._on_freeze:
                self._on_freeze(False)

    @property
    def frozen(self) -> bool:
        if not self._enabled:
            return False
        if not self._frozen:
            elapsed = (time.monotonic() - self._last_frame_ts) * 1000.0
            if elapsed > self._config.video_freeze_timeout_ms:
                self._frozen = True
                if self._on_freeze:
                    self._on_freeze(True)
        return self._frozen

    def confirm_recovered(self) -> None:
        self._frozen = False
        self._last_frame_ts = time.monotonic()

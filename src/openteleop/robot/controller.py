"""Robot-side execution layer.

* :class:`RobotController` - abstract hardware interface (implement per robot)
* :class:`CommandRouter`   - command dispatch, whitelist, velocity limiting,
  and the shared-autonomy residual hook.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

import numpy as np

from ..channels.command import CommandChannel, VelocityLimiter
from ..config.settings import SafetyConfig


class RobotController(ABC):
    """Abstract robot hardware interface.

    Implementations wrap a specific arm/hand (UR + Sharpa Wave, Unitree,
    Franka, ...). The teleoperation stack only ever talks to this interface.
    """

    @property
    @abstractmethod
    def n_dof(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def read_state(self) -> dict:
        """Return current hardware state: joint positions/vels/torques, EE force."""
        raise NotImplementedError

    @abstractmethod
    def send_action(self, action: np.ndarray) -> None:
        """Execute a joint/EE action at the control rate."""
        raise NotImplementedError

    @abstractmethod
    def emergency_stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def ramp_down_to_zero(self, duration_s: float) -> None:
        raise NotImplementedError


class DummyRobot(RobotController):
    """In-memory robot used for demos, tests, and simulation.

    Applies a simple integrator so end-to-end pipelines can be exercised
    without hardware.
    """

    def __init__(self, n_dof: int = 6):
        self._n_dof = n_dof
        self._pos = np.zeros(n_dof, dtype=np.float32)
        self._vel = np.zeros(n_dof, dtype=np.float32)
        self._torque = np.zeros(n_dof, dtype=np.float32)
        self._ee_force = 0.0
        self._lock = threading.Lock()

    @property
    def n_dof(self) -> int:
        return self._n_dof

    def read_state(self) -> dict:
        with self._lock:
            return {
                "joint_positions": self._pos.copy(),
                "joint_velocities": self._vel.copy(),
                "joint_torques": self._torque.copy(),
                "ee_force_mag": self._ee_force,
            }

    def send_action(self, action: np.ndarray) -> None:
        with self._lock:
            target = np.asarray(action, dtype=np.float32).reshape(-1)
            if target.shape[0] != self._n_dof:
                target = np.resize(target, self._n_dof)
            # First-order tracking: move a fraction toward the target.
            self._vel = (target - self._pos) * 0.5
            self._pos = self._pos + self._vel
            self._ee_force = max(0.0, float(np.linalg.norm(self._vel)) * 2.0)

    def emergency_stop(self) -> None:
        with self._lock:
            self._vel[:] = 0.0

    def ramp_down_to_zero(self, duration_s: float) -> None:
        with self._lock:
            self._vel *= 0.1


class SharedAutonomyLayer:
    """Force-residual hook (DexTeleop-0 style).

    Executes on the *robot side* at its own rate and does NOT traverse the
    network. In production this wraps a QP solver that maps fingertip tactile
    error into a joint-space correction ``Delta q``. This reference
    implementation uses a proportional residual with clamping.
    """

    def __init__(self, robot: RobotController, force_limit: float = 10.0, kp: float = 0.1):
        self._robot = robot
        self._force_limit = force_limit
        self._kp = kp

    def add_residual(self, q_teleop: np.ndarray) -> np.ndarray:
        state = self._robot.read_state()
        f = state.get("ee_force_mag", 0.0)
        # If contact force exceeds the limit, back off proportionally.
        if f > self._force_limit:
            scale = max(0.0, 1.0 - self._kp * (f - self._force_limit) / self._force_limit)
            return np.asarray(q_teleop, dtype=np.float32) * scale
        return np.asarray(q_teleop, dtype=np.float32)


class CommandRouter:
    """Robot-side command dispatch.

    High-frequency commands (``cmd_unreliable``) are velocity-limited and fed
    through the shared-autonomy residual. Event commands (``state_reliable``)
    are whitelist-checked and dispatched to registered handlers.
    """

    def __init__(
        self,
        robot: RobotController,
        safety_cfg: SafetyConfig,
        shared_autonomy: Optional[SharedAutonomyLayer] = None,
    ):
        self._robot = robot
        self._safety_cfg = safety_cfg
        self._autonomy = shared_autonomy
        self._limiter = VelocityLimiter(max_delta=safety_cfg.max_command_delta, n_dof=robot.n_dof)
        self._mode = "idle"  # idle / teleop / autonomy
        self._speed_scale = 1.0
        self._force_limit = safety_cfg.ee_force_limit
        self._handlers: Dict[str, Callable[[dict], dict]] = {}
        self._allowed = {
            "e_stop", "set_mode", "reset", "home", "config", "skill_trigger",
        }
        self._last_cmd_ts = 0.0
        self._lock = threading.Lock()

    # ---- high-frequency path ----
    def on_command(self, action_bytes: bytes, timestamp_us: int, seq: int) -> None:
        with self._lock:
            if self._mode != "teleop":
                return
            action, _ts, _seq, _mode, _side = CommandChannel.decode(action_bytes)
            dt = max((time.monotonic() - self._last_cmd_ts), 1 / 1000.0)
            self._last_cmd_ts = time.monotonic()
            limited = self._limiter.limit(action, dt)
            limited = limited * self._speed_scale
            if self._autonomy is not None:
                limited = self._autonomy.add_residual(limited)
            self._robot.send_action(limited)

    # ---- event path ----
    def handle_state(self, message: dict) -> dict:
        cmd_type = message.get("type", "")
        if cmd_type not in self._allowed:
            return {"ok": False, "error": f"command '{cmd_type}' not allowed"}
        handler = self._handlers.get(cmd_type)
        if handler is None:
            return {"ok": False, "error": f"no handler for '{cmd_type}'"}
        try:
            result = handler(message.get("payload", {}))
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def register(self, cmd_type: str, handler: Callable[[dict], dict]) -> None:
        if cmd_type not in self._allowed:
            raise ValueError(f"'{cmd_type}' is not in the whitelist")
        self._handlers[cmd_type] = handler

    # ---- built-in handlers ----
    def _handle_set_mode(self, payload: dict) -> dict:
        new_mode = payload.get("mode")
        if new_mode not in ("idle", "teleop", "autonomy"):
            return {"ok": False, "error": f"invalid mode '{new_mode}'"}
        old_mode = self._mode
        self._mode = new_mode
        self._limiter.reset()
        return {"old_mode": old_mode, "new_mode": new_mode}

    def _handle_config(self, payload: dict) -> dict:
        if "speed_scale" in payload:
            self._speed_scale = max(0.1, min(2.0, float(payload["speed_scale"])))
        if "force_limit" in payload:
            self._force_limit = max(0.5, min(20.0, float(payload["force_limit"])))
        return {"speed_scale": self._speed_scale, "force_limit": self._force_limit}

    def _handle_reset(self, payload: dict) -> dict:
        self._limiter.reset()
        self._mode = "idle"
        return {}

    def _handle_home(self, payload: dict) -> dict:
        self._robot.send_action(np.zeros(self._robot.n_dof, dtype=np.float32))
        return {}

    def _handle_e_stop(self, payload: dict) -> dict:
        level = payload.get("level", "full")
        if level == "full":
            self._robot.emergency_stop()
        else:
            self._robot.ramp_down_to_zero(0.2)
        self._mode = "idle"
        return {"level": level}

    def _handle_skill_trigger(self, payload: dict) -> dict:
        skill_id = payload.get("skill_id")
        if self._autonomy is not None and hasattr(self._autonomy, "trigger_skill"):
            self._autonomy.trigger_skill(skill_id, payload.get("params", {}))
            return {"skill_id": skill_id, "triggered": True}
        return {"ok": False, "error": "shared autonomy not available"}

    def install_defaults(self) -> None:
        """Register the built-in handlers."""
        for name in ("set_mode", "config", "reset", "home", "e_stop", "skill_trigger"):
            self.register(name, getattr(self, f"_handle_{name}"))

    @property
    def mode(self) -> str:
        return self._mode

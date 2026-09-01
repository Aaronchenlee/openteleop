"""High-level channel adapters.

These wrap the transport with the *business* semantics of each channel:

* :class:`CommandChannel`  - rate limiting, velocity limiting, timestamp+seq
* :class:`StateChannel`    - nonce/ack bookkeeping, command whitelist
* :class:`TelemetryChannel` - periodic telemetry publisher
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, Optional

import numpy as np

from ..transport.base import BaseTransport


class VelocityLimiter:
    """Clamps per-tick action deltas to prevent jumps on network jitter.

    Uses first-order hold: the target is the *latest* command, but the
    executed value ramps toward it at a bounded rate.
    """

    def __init__(self, max_delta: float = 0.05, n_dof: int = 6):
        self.max_delta = max_delta
        self.n_dof = n_dof
        self._last: Optional[np.ndarray] = None

    def limit(self, target: np.ndarray, dt: float) -> np.ndarray:
        target = np.asarray(target, dtype=np.float32).reshape(-1)
        if self._last is None or self._last.shape != target.shape:
            self._last = target.copy()
            return target.copy()
        delta = target - self._last
        max_step = self.max_delta * max(dt, 1e-4) * 1000.0  # per ms
        norm = float(np.linalg.norm(delta))
        if norm > max_step and norm > 1e-9:
            delta = delta * (max_step / norm)
        self._last = self._last + delta
        return self._last.copy()

    def reset(self) -> None:
        self._last = None


class CommandChannel:
    """High-frequency control command producer (operator side).

    Responsibilities:
    * serialize actions into the wire format
    * velocity-limit the raw human input
    * stamp with monotonic clock + sequence number
    """

    WIRE_HEADER = 8 + 1 + 1 + 1 + 1  # ts(8) + seq(1) + mode(1) + side(1) + ndof(1)

    def __init__(self, transport: BaseTransport, n_dof: int = 6, max_delta: float = 0.05):
        self._transport = transport
        self._n_dof = n_dof
        self._limiter = VelocityLimiter(max_delta=max_delta, n_dof=n_dof)
        self._seq = 0
        self._last_ts = 0.0
        self._min_interval_s = 0.0  # set from ctrl_hz

    @property
    def ctrl_hz(self) -> float:
        return 1.0 / self._min_interval_s if self._min_interval_s > 0 else 50.0

    @ctrl_hz.setter
    def ctrl_hz(self, value: float) -> None:
        self._min_interval_s = 1.0 / max(value, 1.0)

    @staticmethod
    def encode(action: np.ndarray, ts_us: int, seq: int, mode: int = 0, side: int = 2) -> bytes:
        """Encode a float32 action array into the wire format."""
        action = np.asarray(action, dtype=np.float32).reshape(-1)
        header = ts_us.to_bytes(8, "little") + bytes([seq & 0xFF, mode, side, len(action)])
        return header + action.tobytes()

    @staticmethod
    def decode(payload: bytes) -> tuple[np.ndarray, int, int, int, int]:
        """Decode a wire-format command. Returns (action, ts, seq, mode, side)."""
        ts = int.from_bytes(payload[:8], "little")
        seq, mode, side, ndof = payload[8], payload[9], payload[10], payload[11]
        action = np.frombuffer(payload[12 : 12 + 4 * ndof], dtype=np.float32).copy()
        return action, ts, seq, mode, side

    def send(self, action: np.ndarray, dt: float | None = None) -> None:
        """Rate-limit, velocity-limit, stamp and send one command."""
        now = time.monotonic()
        if self._last_ts and now - self._last_ts < self._min_interval_s:
            return
        self._last_ts = now
        limited = self._limiter.limit(action, dt or self._min_interval_s)
        self._seq = (self._seq + 1) & 0xFF
        ts_us = int(now * 1e6)
        self._transport.send_command(self.encode(limited, ts_us, self._seq), ts_us, self._seq)


class StateChannel:
    """Reliable JSON command channel with nonce/ack and whitelist (robot side)."""

    def __init__(self, transport: BaseTransport, allowed: Optional[set[str]] = None):
        self._transport = transport
        self._allowed = allowed or {
            "e_stop", "set_mode", "reset", "home", "config", "skill_trigger",
        }
        self._handlers: Dict[str, Callable[[dict], dict]] = {}

    def register(self, cmd_type: str, handler: Callable[[dict], dict]) -> None:
        self._handlers[cmd_type] = handler

    def handle(self, message: dict) -> dict:
        """Whitelist check + dispatch. Return value becomes the ack."""
        cmd_type = message.get("type", "")
        if cmd_type not in self._allowed:
            return {"ok": False, "error": f"command '{cmd_type}' not allowed"}
        handler = self._handlers.get(cmd_type)
        if handler is None:
            return {"ok": False, "error": f"no handler for '{cmd_type}'"}
        try:
            result = handler(message.get("payload", {}))
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001 - surface as ack
            return {"ok": False, "error": str(exc)}

    async def send(self, cmd_type: str, payload: dict, timeout_s: float = 2.0) -> dict:
        """Operator side: send a command and await the ack."""
        message = {"type": cmd_type, "nonce": str(uuid.uuid4()), "payload": payload}
        return await self._transport.send_state(message, timeout_s=timeout_s)


class TelemetryChannel:
    """Periodic telemetry publisher (robot side)."""

    def __init__(self, transport: BaseTransport, frequency_hz: float = 5.0):
        self._transport = transport
        self._frequency_hz = frequency_hz
        self._interval_s = 1.0 / frequency_hz
        self._last_sent = 0.0

    @property
    def frequency_hz(self) -> float:
        return self._frequency_hz

    def publish(self, telemetry: dict) -> None:
        now = time.monotonic()
        if now - self._last_sent < self._interval_s:
            return
        self._last_sent = now
        telemetry = {"ts": int(now * 1e6), "stats": self._transport.get_stats(), **telemetry}
        self._transport.send_telemetry(telemetry)

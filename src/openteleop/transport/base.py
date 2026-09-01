"""Transport abstraction layer.

The transport layer decouples business logic from the underlying data path.
Two implementations are provided:

* :class:`LocalZMQTransport` - lowest latency, LAN-only, ZMQ PUB/SUB.
* :class:`RemoteWebRTCTransport` - NAT traversal, internet, WebRTC.

Both expose the same :class:`BaseTransport` interface, so operator / robot
code never needs to know which path is active.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional

from ..config.settings import TeleopConfig

# Callback signatures
CommandCallback = Callable[[bytes, int, int], None]  # (payload, timestamp_us, seq)
StateCallback = Callable[[dict], dict]  # (message) -> ack
TelemetryCallback = Callable[[dict], None]
VideoFrameCallback = Callable[[Any, int], None]  # (frame, timestamp_us)
TactileCallback = Callable[[bytes, int], None]


@dataclass
class ChannelStats:
    """Rolling-window statistics for one channel (dimos LiveStreamStats-style)."""

    window_size: int = 100
    timestamps: Deque[float] = field(default_factory=deque)
    latencies_ms: Deque[float] = field(default_factory=deque)
    received: int = 0
    dropped: int = 0

    @property
    def rate_hz(self) -> float:
        """Estimate arrival frequency from the rolling window."""
        if len(self.timestamps) < 2:
            return 0.0
        span = self.timestamps[-1] - self.timestamps[0]
        return (len(self.timestamps) - 1) / span if span > 0 else 0.0

    @property
    def loss_pct(self) -> float:
        total = self.received + self.dropped
        return (self.dropped / total * 100.0) if total else 0.0

    def observe(self, send_ts_us: int, recv_ts_us: int) -> None:
        """Record one observation. recv_ts is in the same clock domain as send_ts."""
        now = time.monotonic()
        self.timestamps.append(now)
        self.latencies_ms.append((recv_ts_us - send_ts_us) / 1000.0)
        self.received += 1
        if len(self.timestamps) > self.window_size:
            self.timestamps.popleft()
            self.latencies_ms.popleft()

    @property
    def latency_ms(self) -> Optional[float]:
        return self.latencies_ms[-1] if self.latencies_ms else None

    @property
    def jitter_ms(self) -> float:
        """Mean absolute deviation of latency (MAD)."""
        if len(self.latencies_ms) < 2:
            return 0.0
        mean = sum(self.latencies_ms) / len(self.latencies_ms)
        return sum(abs(v - mean) for v in self.latencies_ms) / len(self.latencies_ms)

    @property
    def p95_latency_ms(self) -> Optional[float]:
        if not self.latencies_ms:
            return None
        vals = sorted(self.latencies_ms)
        idx = min(len(vals) - 1, int(len(vals) * 0.95))
        return vals[idx]


class BaseTransport(ABC):
    """Abstract transport interface shared by all implementations.

    The six logical channels (from the dimos Hosted Teleop design) are:

    * ``cmd_unreliable``    operator -> robot, high-frequency control
    * ``state_reliable``    operator -> robot, JSON commands + ack
    * ``state_reliable_back`` robot -> operator, telemetry + acks
    * ``video``             robot -> operator, media stream
    * ``tactile_unreliable`` robot -> operator, force / contact data
    * ``audio``             bidirectional, optional
    """

    def __init__(self, config: TeleopConfig):
        self.config = config
        self._stats: Dict[str, ChannelStats] = {
            name: ChannelStats() for name in self._channel_names()
        }
        self._running = False

    @staticmethod
    def _channel_names() -> list[str]:
        return ["cmd_unreliable", "state_reliable", "state_reliable_back", "video", "tactile_unreliable", "audio"]

    # ---- cmd_unreliable ----
    @abstractmethod
    def send_command(self, action: bytes, timestamp_us: int, seq: int) -> None:
        """Send a high-frequency control command (unreliable, no retransmit)."""
        raise NotImplementedError

    @abstractmethod
    def on_command(self, callback: CommandCallback) -> None:
        """Register the control-command receiver."""
        raise NotImplementedError

    # ---- state_reliable ----
    @abstractmethod
    async def send_state(self, message: dict, timeout_s: float = 2.0) -> dict:
        """Send a reliable state command and wait for the ack."""
        raise NotImplementedError

    @abstractmethod
    def on_state(self, callback: StateCallback) -> None:
        """Register the reliable command handler. Return value is used as ack."""
        raise NotImplementedError

    # ---- state_reliable_back ----
    @abstractmethod
    def send_telemetry(self, telemetry: dict) -> None:
        """Send telemetry back to the operator (reliable)."""
        raise NotImplementedError

    @abstractmethod
    def on_telemetry(self, callback: TelemetryCallback) -> None:
        raise NotImplementedError

    # ---- video ----
    @abstractmethod
    def send_video_frame(self, frame: Any, timestamp_us: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_video_frame(self, callback: VideoFrameCallback) -> None:
        raise NotImplementedError

    # ---- tactile ----
    @abstractmethod
    def send_tactile(self, data: bytes, timestamp_us: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_tactile(self, callback: TactileCallback) -> None:
        raise NotImplementedError

    # ---- lifecycle ----
    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def wait_closed(self) -> None:
        raise NotImplementedError

    def get_stats(self) -> Dict[str, dict]:
        """Snapshot all channel statistics for telemetry display."""
        return {name: {
            "rate_hz": round(s.rate_hz, 1),
            "latency_ms": round(s.latency_ms, 2) if s.latency_ms is not None else None,
            "p95_latency_ms": round(s.p95_latency_ms, 2) if s.p95_latency_ms is not None else None,
            "jitter_ms": round(s.jitter_ms, 2),
            "loss_pct": round(s.loss_pct, 2),
        } for name, s in self._stats.items()}

"""Multimodal time-synchronization buffer.

Implements the LiveKit Portal-style ``SyncBuffer``: all outbound data is
stamped with the sender's monotonic clock; the receiver aligns the slowest
channel (video) with the nearest samples of the other channels instead of
using "latest frame + latest state" (which would show misalignment of
30-50 ms between video and control-state paths).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional


@dataclass
class Observation:
    """A time-aligned multimodal observation."""

    timestamp_us: int
    frames: Dict[str, Any] = field(default_factory=dict)
    state: Optional[dict] = None
    tactile: Optional[bytes] = None


class SyncBuffer:
    """Align incoming video/state/tactile by sender timestamps.

    Parameters
    ----------
    video_fps:
        Nominal video rate, used to derive the search window.
    tolerance_ticks:
        Search window expressed in frame periods. Default 1.5 -> 50 ms @30fps.
    on_observation:
        Called with an :class:`Observation` once a frame is aligned.
    """

    def __init__(
        self,
        video_fps: float = 30.0,
        tolerance_ticks: float = 1.5,
        on_observation: Optional[Callable[[Observation], None]] = None,
    ):
        self.search_range_us = int(tolerance_ticks / max(video_fps, 1.0) * 1_000_000)
        self._frames: Deque[tuple[int, str, Any]] = deque(maxlen=60)
        self._states: Deque[tuple[int, dict]] = deque(maxlen=200)
        self._tactiles: Deque[tuple[int, bytes]] = deque(maxlen=200)
        self.on_observation = on_observation
        self.dropped = 0

    def add_frame(self, cam_name: str, frame: Any, timestamp_us: int) -> None:
        self._frames.append((timestamp_us, cam_name, frame))
        self._try_emit()

    def add_state(self, state: dict, timestamp_us: int) -> None:
        self._states.append((timestamp_us, state))

    def add_tactile(self, data: bytes, timestamp_us: int) -> None:
        self._tactiles.append((timestamp_us, data))

    def _try_emit(self) -> None:
        if not self._frames:
            return
        frame_ts, cam_name, frame = self._frames[-1]
        state = self._find_nearest(self._states, frame_ts)
        tactile = self._find_nearest(self._tactiles, frame_ts)

        # If the newest state is already older than the frame's window,
        # emit anyway with None (stale state is better than blocking the loop).
        newest_state_ts = self._states[-1][0] if self._states else 0
        if state is None and newest_state_ts >= frame_ts - self.search_range_us:
            return  # more recent state may still arrive; wait for the next frame

        if self.on_observation:
            self.on_observation(
                Observation(
                    timestamp_us=frame_ts,
                    frames={cam_name: frame},
                    state=state[1] if state else None,
                    tactile=tactile[1] if tactile else None,
                )
            )

    def _find_nearest(self, buf: Deque[tuple[int, Any]], target_ts: int):
        best: Optional[tuple[int, Any]] = None
        best_diff = float("inf")
        for ts, data in buf:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = (ts, data)
            if ts > target_ts + self.search_range_us:
                break  # timestamps are monotonic; no need to scan further
        if best is not None and best_diff <= self.search_range_us:
            return best
        self.dropped += 1
        return None

    def clear(self) -> None:
        self._frames.clear()
        self._states.clear()
        self._tactiles.clear()

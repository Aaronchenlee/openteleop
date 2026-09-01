"""Example: programmatic API — inspect protocol wire format and SyncBuffer."""

from __future__ import annotations

import numpy as np

from openteleop.channels.command import CommandChannel
from openteleop.sync.sync_buffer import SyncBuffer


def main() -> None:
    # 1. Command wire format
    action = np.array([0.1, -0.2, 0.3, 0.4, 0.5, 0.6], dtype=np.float32)
    payload = CommandChannel.encode(action, ts_us=123456, seq=9, mode=0, side=2)
    print(f"Encoded {len(payload)} bytes: {payload[:12].hex()} ...")

    decoded, ts, seq, mode, side = CommandChannel.decode(payload)
    print(f"Decoded: ts={ts} seq={seq} mode={mode} side={side} action={decoded}")

    # 2. SyncBuffer alignment
    obs = []
    buf = SyncBuffer(video_fps=30.0, on_observation=obs.append)
    buf.add_state({"mode": "teleop"}, 100_000)
    buf.add_tactile(b"\x01\x02", 101_000)
    buf.add_frame("main", "FRAME", 100_500)
    print(f"Aligned observations: {len(obs)}")
    if obs:
        o = obs[0]
        print(f"  frames={list(o.frames.keys())} state={o.state} tactile_len={len(o.tactile) if o.tactile else 0}")


if __name__ == "__main__":
    main()

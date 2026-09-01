"""Example: full operator + robot pipeline on one host (loopback ZMQ).

Run:
    python examples/demo_end_to_end.py

Shows: session startup, mode switch, command flow, telemetry, shutdown.
"""
from __future__ import annotations

import asyncio

from openteleop.config.settings import TeleopConfig, TransportMode
from openteleop.operator.input_adapter import SineWaveAdapter
from openteleop.operator.session import OperatorSession
from openteleop.robot.session import RobotSession
from openteleop.transport.local_zmq import LocalZMQTransport


async def main() -> None:
    cfg = TeleopConfig.default_robot()
    cfg.mode = TransportMode.LOCAL
    cfg.ctrl_hz = 50.0
    cfg.n_dof = 6
    # This demo has no video stream, so the L4 video-freeze gate would block
    # operator input. Disable it (production teleop keeps it ON).
    cfg.safety.enable_video_guard = False

    robot = RobotSession(
        cfg, transport=LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1")
    )
    op = OperatorSession(
        cfg,
        transport=LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1"),
        input_adapter=SineWaveAdapter(n_dof=6, freq_hz=0.8),
    )

    await robot.start()
    await op.start()

    print("Sessions started. Enabling teleop mode...")
    ack = await op.send_command("set_mode", {"mode": "teleop"})
    print(f"set_mode ack: {ack}")

    await asyncio.sleep(2.0)

    print("Command channel stats:", op.get_stats()["cmd_unreliable"])
    pos = robot.robot.read_state()["joint_positions"]
    print("Robot joint positions:", pos)
    print("Latest telemetry keys:", list(op.telemetry.keys()) if op.telemetry else "none yet")

    ack = await op.send_command("set_mode", {"mode": "idle"})
    print(f"set_mode idle ack: {ack}")

    await op.stop()
    await robot.stop()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())

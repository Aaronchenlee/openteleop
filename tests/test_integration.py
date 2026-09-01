"""End-to-end integration test: operator + robot over loopback ZMQ."""

from __future__ import annotations

import asyncio

import pytest

from openteleop.config.settings import TeleopConfig, TransportMode
from openteleop.operator.input_adapter import SineWaveAdapter
from openteleop.operator.session import OperatorSession
from openteleop.robot.session import RobotSession
from openteleop.transport.local_zmq import LocalZMQTransport

from .conftest import make_isolated_config


async def test_end_to_end_loopback(isolated_ports):
    """Full operator -> robot pipeline over loopback ZMQ."""
    cfg = make_isolated_config(isolated_ports)
    cfg.ctrl_hz = 50.0
    cfg.n_dof = 6
    # No video stream in this pipeline - disable the video-freeze gate.
    cfg.safety.enable_video_guard = False

    robot = RobotSession(cfg)
    op = OperatorSession(cfg, input_adapter=SineWaveAdapter(n_dof=6, freq_hz=0.5))

    # Explicit loopback transports with matching roles.
    robot._transport = LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1")
    op._transport = LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1")

    await robot.start()
    await op.start()

    # Enable teleop mode.
    ack = await op.send_command("set_mode", {"mode": "teleop"})
    assert ack["ok"] is True, f"set_mode failed: {ack}"

    # Let it run for ~1s so commands flow.
    await asyncio.sleep(1.0)

    # The dummy robot should have moved.
    pos = robot.robot.read_state()["joint_positions"]
    assert not all(abs(v) < 1e-6 for v in pos), f"robot did not move: {pos}"

    # Telemetry should be arriving at the operator.
    assert op.telemetry, "no telemetry received at operator"

    # Switch back to idle.
    ack = await op.send_command("set_mode", {"mode": "idle"})
    assert ack["ok"] is True

    await op.stop()
    await robot.stop()

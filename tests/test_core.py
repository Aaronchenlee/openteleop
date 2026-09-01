"""Tests for channels, sync buffer, safety, and robot-side logic."""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from openteleop.channels.command import CommandChannel, StateChannel, VelocityLimiter
from openteleop.config.settings import SafetyConfig, TeleopConfig
from openteleop.robot.controller import CommandRouter, DummyRobot, SharedAutonomyLayer
from openteleop.safety.safety import HeartbeatMonitor, SafetyMonitor, VideoGuard
from openteleop.sync.sync_buffer import Observation, SyncBuffer


# ---- command channel ----
def test_command_encode_decode_roundtrip():
    action = np.array([0.1, -0.2, 0.3, 0.4, 0.5, 0.6], dtype=np.float32)
    payload = CommandChannel.encode(action, ts_us=123456, seq=9, mode=0, side=2)
    decoded, ts, seq, mode, side = CommandChannel.decode(payload)
    np.testing.assert_allclose(decoded, action, atol=1e-6)
    assert ts == 123456
    assert seq == 9
    assert mode == 0
    assert side == 2


def test_velocity_limiter_clamps_jump():
    limiter = VelocityLimiter(max_delta=0.05, n_dof=1)
    target = np.array([1.0])
    # First call passes through.
    first = limiter.limit(target, dt=0.02)
    assert first[0] == 1.0
    # A huge jump is clamped.
    jumped = limiter.limit(np.array([5.0]), dt=0.02)
    assert abs(jumped[0] - 1.0) <= 0.05 * 20 + 1e-6


# ---- sync buffer ----
def test_sync_buffer_aligns_channels():
    obs: list[Observation] = []
    buf = SyncBuffer(video_fps=30.0, on_observation=obs.append)
    # State and tactile close in time to the frame.
    buf.add_state({"mode": "teleop"}, 100_000)
    buf.add_tactile(b"\x01\x02", 101_000)
    buf.add_frame("main", object(), 100_500)
    assert len(obs) == 1
    assert obs[0].state is not None
    assert obs[0].tactile is not None


def test_sync_buffer_drops_stale_state():
    obs: list[Observation] = []
    buf = SyncBuffer(video_fps=30.0, tolerance_ticks=1.5, on_observation=obs.append)
    buf.add_state({"mode": "idle"}, 0)  # very old
    buf.add_frame("main", object(), 10_000_000)  # 10s later
    # Newest state is already far behind the frame window -> emit with None.
    assert len(obs) == 1
    assert obs[0].state is None


# ---- safety ----
def test_heartbeat_deadman():
    cfg = SafetyConfig(heartbeat_timeout_ms=200, heartbeat_hz=5.0)
    fired = []
    hb = HeartbeatMonitor(cfg, on_deadman=lambda: fired.append(True))
    hb.start()
    hb.on_heartbeat()
    time.sleep(0.5)  # exceeds 200ms timeout
    assert hb.alive is False
    assert fired
    hb.stop()


def test_safety_monitor_violation():
    cfg = SafetyConfig(joint_vel_limit=3.0)
    mon = SafetyMonitor(cfg)
    assert mon._check({"joint_vels": [1.0]}) is None
    assert mon._check({"joint_vels": [5.0]}) == "joint_vel_limit"


def test_video_guard_freeze_and_recover():
    cfg = SafetyConfig(video_freeze_timeout_ms=150)
    guard = VideoGuard(cfg)
    guard.on_frame()
    assert guard.frozen is False
    time.sleep(0.3)
    assert guard.frozen is True
    guard.on_frame()
    assert guard.frozen is False


# ---- robot side ----
def test_command_router_teleop_flow():
    cfg = TeleopConfig.default_robot()
    robot = DummyRobot(n_dof=6)
    router = CommandRouter(robot, cfg.safety, SharedAutonomyLayer(robot))
    router.install_defaults()

    # Not in teleop mode -> commands ignored.
    action = np.zeros(6, dtype=np.float32)
    payload = CommandChannel.encode(action, 0, 1, mode=0, side=2)
    router.on_command(payload, 0, 1)
    assert np.all(robot.read_state()["joint_positions"] == 0)

    # Switch to teleop and send a target.
    ack = router.handle_state({"type": "set_mode", "payload": {"mode": "teleop"}})
    assert ack["ok"] is True
    target = np.full(6, 0.5, dtype=np.float32)
    payload = CommandChannel.encode(target, 0, 2, mode=0, side=2)
    router.on_command(payload, 0, 2)
    state = robot.read_state()
    assert not np.allclose(state["joint_positions"], 0)


def test_command_router_whitelist():
    cfg = TeleopConfig.default_robot()
    robot = DummyRobot(n_dof=6)
    router = CommandRouter(robot, cfg.safety)
    ack = router.handle_state({"type": "rm_rf", "payload": {}})
    assert ack["ok"] is False
    assert "not allowed" in ack["error"]


def test_estop_halts_robot():
    cfg = TeleopConfig.default_robot()
    robot = DummyRobot(n_dof=6)
    router = CommandRouter(robot, cfg.safety)
    router.install_defaults()
    router.handle_state({"type": "set_mode", "payload": {"mode": "teleop"}})
    target = np.full(6, 0.5, dtype=np.float32)
    payload = CommandChannel.encode(target, 0, 1, mode=0, side=2)
    router.on_command(payload, 0, 1)
    vel_before = robot.read_state()["joint_velocities"].copy()
    assert np.linalg.norm(vel_before) > 0
    router.handle_state({"type": "e_stop", "payload": {"level": "full"}})
    assert np.all(robot.read_state()["joint_velocities"] == 0)

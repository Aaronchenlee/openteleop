"""Shared test fixtures - random ports to isolate parallel/sequential tests."""

from __future__ import annotations

import socket

import pytest

from openteleop.config.settings import ChannelConfig, TeleopConfig, TransportMode


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def isolated_ports():
    """Return a unique set of six channel ports for one test."""
    base = _free_port()
    return {
        "cmd_unreliable": base,
        "state_reliable": base + 1,
        "state_reliable_back": base + 2,
        "video": base + 3,
        "tactile_unreliable": base + 4,
        "audio": base + 5,
    }


def make_isolated_config(ports: dict, *, role_hint: str = "robot") -> TeleopConfig:
    """Build a TeleopConfig whose six channels use the given ports."""
    cfg = TeleopConfig.default_robot()
    cfg.mode = TransportMode.LOCAL
    cfg.host = "127.0.0.1"
    cfg.channels = [
        ChannelConfig(name="cmd_unreliable", port=ports["cmd_unreliable"], hwm=10, frequency_hz=50.0),
        ChannelConfig(name="state_reliable", port=ports["state_reliable"], hwm=100, frequency_hz=10.0, reliable=True, ordered=True),
        ChannelConfig(name="state_reliable_back", port=ports["state_reliable_back"], hwm=100, frequency_hz=10.0, reliable=True, ordered=True),
        ChannelConfig(name="video", port=ports["video"], hwm=30, frequency_hz=30.0),
        ChannelConfig(name="tactile_unreliable", port=ports["tactile_unreliable"], hwm=10, frequency_hz=100.0),
        ChannelConfig(name="audio", port=ports["audio"], hwm=30, frequency_hz=30.0),
    ]
    return cfg

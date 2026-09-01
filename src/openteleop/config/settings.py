"""OpenTeleop configuration models."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class TransportMode(str, Enum):
    """Transport mode selection."""

    AUTO = "auto"
    LOCAL = "local"
    REMOTE = "remote"


class VideoCodec(str, Enum):
    """Supported video codecs (hardware acceleration preferred)."""

    VP8 = "vp8"
    H264 = "h264"
    VP9 = "vp9"


class ChannelConfig(BaseModel):
    """Configuration for a single logical channel."""

    name: str
    port: int = Field(ge=1024, le=65535)
    hwm: int = Field(default=10, ge=1, description="High-water mark, drop-oldest behavior")
    frequency_hz: float = Field(default=50.0, gt=0)
    reliable: bool = False
    ordered: bool = False


class VideoConfig(BaseModel):
    """Video pipeline configuration."""

    codec: VideoCodec = VideoCodec.H264
    width: int = 1280
    height: int = 720
    fps: int = 30
    bitrate_bps: int = 2_000_000
    min_bitrate_bps: int = 500_000
    max_bitrate_bps: int = 6_000_000
    hardware_encode: bool = True
    mux_cameras: bool = True
    adaptive_bitrate: bool = True


class SafetyConfig(BaseModel):
    """Safety thresholds (L1-L4)."""

    heartbeat_hz: float = 10.0
    heartbeat_timeout_ms: int = 500
    ramp_down_ms: int = 200
    video_freeze_timeout_ms: int = 300
    enable_video_guard: bool = Field(
        default=True,
        description="Block operator input when video freezes. Disable for "
        "pure-command / simulation / data-replay pipelines without a video stream.",
    )
    joint_vel_limit: float = 3.0  # rad/s
    joint_torque_limit: float = 30.0  # N*m
    ee_force_limit: float = 10.0  # N
    max_command_delta: float = 0.05  # per control tick
    check_interval_ms: int = 1


class RetargetConfig(BaseModel):
    """Motion retargeting configuration."""

    solver: str = "slsqp"
    max_solve_ms: float = 15.0
    w_pos: float = 1.0
    w_dir: float = 0.5
    w_reg: float = 0.1
    w_escape: float = 10.0
    enable_collision_avoidance: bool = True


class TeleopConfig(BaseModel):
    """Top-level teleoperation configuration."""

    mode: TransportMode = TransportMode.AUTO
    host: str = "0.0.0.0"
    operator_id: str = "operator-001"
    robot_id: str = "robot-001"
    n_dof: int = 6
    n_fingers: int = 5
    ctrl_hz: float = 50.0
    telemetry_hz: float = 5.0
    warmup_ms: int = Field(
        default=200,
        ge=0,
        description="Post-connect publisher warm-up in ms. ZMQ PUB/SUB drops "
        "messages sent before late-joining subscribers finish their reconnect "
        "handshake (slow-joiner); this brief settle window avoids losing the "
        "first command burst after session bring-up.",
    )
    link_upstream_bps: int = Field(
        default=0,
        ge=0,
        description="Network-side upstream bandwidth budget (edge->cloud, bps). "
        "0 = unconstrained. Used by adapter negotiation as a hard ceiling.",
    )
    link_downstream_bps: int = Field(
        default=0,
        ge=0,
        description="Network-side downstream bandwidth budget (cloud->edge, bps). "
        "0 = unconstrained. Used by adapter negotiation as a hard ceiling.",
    )
    signaling_url: Optional[str] = None
    stun_servers: List[str] = Field(
        default_factory=lambda: ["stun:stun.l.google.com:19302"]
    )
    turn_servers: List[dict] = Field(default_factory=list)
    channels: List[ChannelConfig] = Field(default_factory=list)
    video: VideoConfig = Field(default_factory=VideoConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    retarget: RetargetConfig = Field(default_factory=RetargetConfig)

    @classmethod
    def from_file(cls, path: str | Path) -> "TeleopConfig":
        """Load config from a YAML/JSON file."""
        import json

        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**data)

    @classmethod
    def default_robot(cls) -> "TeleopConfig":
        """Default robot-side configuration with the six-channel layout."""
        return cls(
            channels=[
                ChannelConfig(name="cmd_unreliable", port=5555, hwm=10, frequency_hz=50.0, reliable=False, ordered=False),
                ChannelConfig(name="state_reliable", port=5556, hwm=100, frequency_hz=10.0, reliable=True, ordered=True),
                ChannelConfig(name="state_reliable_back", port=5557, hwm=100, frequency_hz=10.0, reliable=True, ordered=True),
                ChannelConfig(name="video", port=5558, hwm=30, frequency_hz=30.0, reliable=False, ordered=False),
                ChannelConfig(name="tactile_unreliable", port=5559, hwm=10, frequency_hz=100.0, reliable=False, ordered=False),
                ChannelConfig(name="audio", port=5560, hwm=30, frequency_hz=30.0, reliable=False, ordered=False),
            ]
        )

    @classmethod
    def default_operator(cls) -> "TeleopConfig":
        """Default operator-side configuration."""
        return cls.default_robot()

"""QoS -> channel mapping: default six channels + dynamic topic channel pool.

The adapter translates an application's :class:`QoSDimensions` into a concrete
channel binding (port, bind owner, HWM, pattern). Fixed channels are preferred
when the QoS matches their semantics; otherwise a dynamic channel is allocated
from a port pool, and the port/binding table is exchanged during negotiation so
both ends configure identical transports.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Dict, Optional

from ..config.settings import TeleopConfig
from ..transport.local_zmq import DEFAULT_PORTS
from .qos import QoSDimensions, Reliability


# Fixed channels and their native semantics (used for semantic matching).
@dataclass(frozen=True)
class FixedChannel:
    name: str
    owner: str  # who binds
    pattern: str  # PUB or REP
    kind: str  # cmd | state | telemetry | video | tactile | audio


FIXED_CHANNELS = {
    "cmd_unreliable": FixedChannel("cmd_unreliable", "operator", "PUB", "cmd"),
    "state_reliable": FixedChannel("state_reliable", "robot", "REP", "state"),
    "state_reliable_back": FixedChannel("state_reliable_back", "robot", "PUB", "telemetry"),
    "video": FixedChannel("video", "robot", "PUB", "video"),
    "tactile_unreliable": FixedChannel("tactile_unreliable", "robot", "PUB", "tactile"),
    "audio": FixedChannel("audio", "robot", "PUB", "audio"),
}


def default_channel_port(name: str) -> int:
    return DEFAULT_PORTS.get(name, 0)


class ChannelMap:
    """Maps topic + QoS to a concrete channel binding (fixed or dynamic)."""

    def __init__(self, config: TeleopConfig, dynamic_port_base: int = 6100):
        self._config = config
        self._dynamic_port = itertools.count(dynamic_port_base)
        # topic -> binding
        self._bindings: Dict[str, ChannelBinding] = {}
        # used dynamic ports to avoid collisions
        self._used_ports: set[int] = set()
        # reserve fixed ports
        for name, fixed in FIXED_CHANNELS.items():
            p = _fixed_port(config, name)
            if p:
                self._used_ports.add(p)

    # ---- resolution ----
    def resolve(self, topic: str, qos: QoSDimensions, role: str) -> "ChannelBinding":
        """Return (and register) a binding for a topic given its QoS."""
        if topic in self._bindings:
            return self._bindings[topic]
        fixed = self._match_fixed(topic, qos)
        if fixed is not None:
            binding = ChannelBinding(
                topic=topic,
                channel_name=fixed.name,
                port=_fixed_port(self._config, fixed.name),
                owner=fixed.owner,
                pattern=fixed.pattern,
                dynamic=False,
                qos=qos,
            )
        else:
            binding = self._alloc_dynamic(topic, qos, role)
        self._bindings[topic] = binding
        return binding

    def _match_fixed(self, topic: str, qos: QoSDimensions) -> Optional[FixedChannel]:
        """Conservative semantic matching against the fixed six channels.

        Custom / application-defined topics default to the dynamic channel pool
        unless the QoS *unambiguously* matches a fixed channel's native role:
        reliable commands ride REQ/REP, video-grade upstream rides the video
        channel. Everything else gets a dedicated dynamic channel.
        """
        if qos.reliability == Reliability.RELIABLE:
            return FIXED_CHANNELS["state_reliable"]
        if qos.upstream_bps >= 1_000_000:  # video-grade upstream bandwidth
            return FIXED_CHANNELS["video"]
        return None

    def _alloc_dynamic(self, topic: str, qos: QoSDimensions, role: str) -> "ChannelBinding":
        port = self._next_free_port()
        # Data direction decides bind owner: upstream-heavy topics are bound by
        # the robot (like video/tactile), downstream by the operator (like cmd).
        owner = "robot" if qos.upstream_bps >= qos.downstream_bps else "operator"
        return ChannelBinding(
            topic=topic,
            channel_name=f"dyn_{topic.replace('/', '_')}",
            port=port,
            owner=owner,
            pattern="PUB",
            dynamic=True,
            qos=qos,
        )

    def _next_free_port(self) -> int:
        while True:
            port = next(self._dynamic_port)
            if port not in self._used_ports:
                self._used_ports.add(port)
                return port

    def register(self, binding: "ChannelBinding") -> None:
        self._bindings[binding.topic] = binding

    def get(self, topic: str) -> Optional["ChannelBinding"]:
        return self._bindings.get(topic)

    def bindings(self) -> Dict[str, "ChannelBinding"]:
        return dict(self._bindings)

    def to_negotiation_payload(self) -> dict:
        """Serializable topic -> {port, owner, qos} table for handshake."""
        return {
            topic: {
                "channel": b.channel_name,
                "port": b.port,
                "owner": b.owner,
                "qos": b.qos.as_dict(),
            }
            for topic, b in self._bindings.items()
        }


@dataclass(frozen=True)
class ChannelBinding:
    """Concrete resolved binding for one topic."""

    topic: str
    channel_name: str
    port: int
    owner: str  # bind owner: "robot" or "operator"
    pattern: str  # "PUB" (dynamic) or fixed
    dynamic: bool
    qos: QoSDimensions


def _fixed_port(config: TeleopConfig, name: str) -> int:
    chan = next((c for c in config.channels if c.name == name), None)
    return chan.port if chan else DEFAULT_PORTS.get(name, 0)

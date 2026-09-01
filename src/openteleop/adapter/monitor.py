"""Runtime QoS monitoring and automatic degradation.

The monitor samples actual channel stats (rate / latency / jitter / loss /
bandwidth) and compares them against the negotiated :class:`QoSDimensions`.
On violation it walks a degradation ladder (rate down, bitrate down, packet
size down, reliability up); when the ladder bottom is reached it raises an
``on_qos_breach`` callback so the application can act (e.g. switch transport).
When metrics recover, QoS is restored one step at a time to avoid oscillation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .qos import QoSDimensions, Reliability


# Degradation ladder per violated dimension.
_RATE_LADDER_HZ = [500.0, 200.0, 100.0, 50.0, 20.0, 10.0]
_BITRATE_LADDER = [0.8, 0.6, 0.4, 0.25, 0.15, 0.1]  # fraction of nominal
_PACKET_LADDER = [0.8, 0.6, 0.4, 0.25, 0.15, 0.1]


@dataclass
class QoSMetric:
    """Measured values for one topic channel."""

    rate_hz: float = 0.0
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    bps: float = 0.0
    sample_count: int = 0


@dataclass
class Violation:
    topic: str
    dimension: str  # rate | latency | jitter | loss | bandwidth
    expected: float
    actual: float


@dataclass
class DegradedState:
    """Current degradation state for one topic."""

    rate_idx: int = 0
    bitrate_idx: int = 0
    packet_idx: int = 0
    reliability: Reliability = Reliability.BEST_EFFORT
    violations_since_recover: int = 0

    def active(self) -> bool:
        return self.rate_idx > 0 or self.bitrate_idx > 0 or self.packet_idx > 0


class QoSDegrader:
    """Detects violations and computes per-topic degradation steps."""

    def __init__(
        self,
        expected: Dict[str, QoSDimensions],
        auto_degrade: bool = True,
        on_breach: Optional[Callable[[str, str, float, float], None]] = None,
    ):
        self._expected = expected
        self._auto = auto_degrade
        self._on_breach = on_breach
        self._states: Dict[str, DegradedState] = {
            t: DegradedState() for t in expected
        }

    def check(self, metrics: Dict[str, QoSMetric]) -> List[Violation]:
        """Compare measured metrics against expected QoS; apply degradation."""
        violations: List[Violation] = []
        for topic, exp in self._expected.items():
            m = metrics.get(topic)
            if m is None or m.sample_count == 0:
                continue
            state = self._states[topic]
            eff = self._effective(topic, exp, state)
            v = self._detect(topic, eff, m)
            if v:
                violations.append(v)
                state.violations_since_recover += 1
                if self._auto:
                    self._degrade(topic, state)
                    if self._on_breach:
                        self._on_breach(v.topic, v.dimension, v.expected, v.actual)
            else:
                state.violations_since_recover = 0
                if state.active():
                    self._recover(topic, state)
        return violations

    # ---- effective (degraded) contract ----
    def _effective(self, topic: str, exp: QoSDimensions, st: DegradedState) -> QoSDimensions:
        rate = exp.rate_hz
        if st.rate_idx > 0:
            rate = _RATE_LADDER_HZ[min(st.rate_idx, len(_RATE_LADDER_HZ) - 1)]
        up = exp.upstream_bps
        if st.bitrate_idx > 0:
            up = int(exp.upstream_bps * _BITRATE_LADDER[min(st.bitrate_idx, len(_BITRATE_LADDER) - 1)])
        pkt = exp.packet_size_bytes
        if st.packet_idx > 0:
            pkt = int(exp.packet_size_bytes * _PACKET_LADDER[min(st.packet_idx, len(_PACKET_LADDER) - 1)])
        rel = st.reliability if st.reliability is not None else exp.reliability
        return QoSDimensions(
            rate_hz=rate,
            max_latency_ms=exp.max_latency_ms,
            max_jitter_ms=exp.max_jitter_ms,
            upstream_bps=up,
            downstream_bps=exp.downstream_bps,
            packet_size_bytes=pkt,
            reliability=rel,
            ordering=exp.ordering,
            max_loss_pct=exp.max_loss_pct,
            priority=exp.priority,
        )

    def _detect(self, topic: str, eff: QoSDimensions, m: QoSMetric) -> Optional[Violation]:
        if eff.rate_hz and m.rate_hz and m.rate_hz < eff.rate_hz * 0.8:
            return Violation(topic, "rate", eff.rate_hz, m.rate_hz)
        if eff.max_latency_ms and m.latency_ms > eff.max_latency_ms:
            return Violation(topic, "latency", eff.max_latency_ms, m.latency_ms)
        if eff.max_jitter_ms and m.jitter_ms > eff.max_jitter_ms:
            return Violation(topic, "jitter", eff.max_jitter_ms, m.jitter_ms)
        if eff.max_loss_pct and m.loss_pct > eff.max_loss_pct:
            return Violation(topic, "loss", eff.max_loss_pct, m.loss_pct)
        if eff.upstream_bps and m.bps > eff.upstream_bps * 1.2:
            return Violation(topic, "bandwidth", eff.upstream_bps, m.bps)
        return None

    # ---- ladder ----
    def _degrade(self, topic: str, st: DegradedState) -> None:
        # Reliability upgrade first (cheapest single knob for loss).
        if st.reliability == Reliability.BEST_EFFORT:
            st.reliability = Reliability.PARTIAL
            return
        if st.reliability == Reliability.PARTIAL:
            st.reliability = Reliability.RELIABLE
            return
        # Then reduce rate until the floor.
        if st.rate_idx < len(_RATE_LADDER_HZ) - 1:
            st.rate_idx += 1
            return
        # Then reduce bitrate, then packet size.
        if st.bitrate_idx < len(_BITRATE_LADDER) - 1:
            st.bitrate_idx += 1
            return
        if st.packet_idx < len(_PACKET_LADDER) - 1:
            st.packet_idx += 1

    def _recover(self, topic: str, st: DegradedState) -> None:
        """Step back up one rung once metrics are healthy (anti-oscillation)."""
        if st.packet_idx > 0:
            st.packet_idx -= 1
        elif st.bitrate_idx > 0:
            st.bitrate_idx -= 1
        elif st.rate_idx > 0:
            st.rate_idx -= 1
        elif st.reliability != Reliability.BEST_EFFORT:
            st.reliability = Reliability.BEST_EFFORT

    def state(self, topic: str) -> DegradedState:
        return self._states[topic]

    def effective(self, topic: str) -> QoSDimensions:
        st = self._states[topic]
        return self._effective(topic, self._expected[topic], st)


def metrics_from_stats(topic: str, stats: dict) -> Optional[QoSMetric]:
    """Adapt a transport ChannelStats dict into a QoSMetric."""
    rate = stats.get("rate_hz", 0.0) or 0.0
    lat = stats.get("p95_latency_ms") or stats.get("latency_ms") or 0.0
    jit = stats.get("jitter_ms", 0.0) or 0.0
    loss = stats.get("loss_pct", 0.0) or 0.0
    if rate == 0.0:
        return None
    return QoSMetric(
        rate_hz=rate,
        latency_ms=float(lat),
        jitter_ms=float(jit),
        loss_pct=float(loss),
        sample_count=1,
    )

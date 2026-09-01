"""QoS dimensions & reliability model for the adapter layer.

A :class:`QoSDimensions` is the contract an application declares per topic.
It captures the eight dimensions agreed in the design review:

* frequency (``rate_hz``)
* latency (``max_latency_ms``) and jitter (``max_jitter_ms``)
* upstream / downstream bandwidth (``upstream_bps`` / ``downstream_bps``)
* packet size (``packet_size_bytes``)
* reliability (``Reliability``) and ordering
* priority

Bandwidth direction is fixed by convention:

* **upstream** = edge (robot) -> cloud (operator): state / video / tactile / telemetry
* **downstream** = cloud -> edge: command / config / e-stop
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Reliability(str, Enum):
    """Reliability level of a channel."""

    BEST_EFFORT = "best_effort"  # fire-and-forget: may drop, reorder, no retransmit
    PARTIAL = "partial"          # bounded retransmit budget (e.g. retry once)
    RELIABLE = "reliable"        # no-loss + ordered, request-ack semantics


# Severity order used by negotiation ("take the stricter one").
_RELIABILITY_RANK = {
    Reliability.BEST_EFFORT: 0,
    Reliability.PARTIAL: 1,
    Reliability.RELIABLE: 2,
}


class QoSError(ValueError):
    """Raised when a QoS declaration is invalid or impossible to map."""


@dataclass(frozen=True)
class QoSDimensions:
    """Application-facing QoS contract for one topic."""

    # frequency
    rate_hz: float = 0.0  # 0 = event-driven / no rate target
    # latency
    max_latency_ms: int = 0  # 0 = unconstrained
    max_jitter_ms: int = 0   # 0 = unconstrained
    # bandwidth (direction semantics fixed: upstream=edge->cloud)
    upstream_bps: int = 0    # 0 = unconstrained
    downstream_bps: int = 0  # 0 = unconstrained
    # packet size
    packet_size_bytes: int = 0  # 0 = unconstrained
    # reliability
    reliability: Reliability = Reliability.BEST_EFFORT
    ordering: bool = True
    max_loss_pct: float = 0.0
    priority: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Reject nonsensical QoS declarations (fast-fail at advertise time)."""
        if self.rate_hz < 0:
            raise QoSError(f"rate_hz must be >= 0, got {self.rate_hz}")
        if self.max_latency_ms < 0:
            raise QoSError(f"max_latency_ms must be >= 0, got {self.max_latency_ms}")
        if self.max_jitter_ms < 0:
            raise QoSError(f"max_jitter_ms must be >= 0, got {self.max_jitter_ms}")
        if self.upstream_bps < 0 or self.downstream_bps < 0:
            raise QoSError("bandwidth must be >= 0")
        if self.packet_size_bytes < 0:
            raise QoSError("packet_size_bytes must be >= 0")
        if not 0.0 <= self.max_loss_pct <= 100.0:
            raise QoSError("max_loss_pct must be in [0, 100]")
        if self.reliability == Reliability.RELIABLE and self.max_loss_pct > 0:
            raise QoSError(
                "RELIABLE channels cannot tolerate loss; set max_loss_pct=0"
            )

    # ---- negotiation ----
    def stricter(self, other: "QoSDimensions") -> "QoSDimensions":
        """Merge two QoS declarations, taking the stricter value per dimension.

        Used in end-to-end negotiation so that neither side can silently ask
        for more than the other can provide.
        """
        rel = (
            self.reliability
            if _RELIABILITY_RANK[self.reliability] >= _RELIABILITY_RANK[other.reliability]
            else other.reliability
        )
        return QoSDimensions(
            rate_hz=_min_nonzero(self.rate_hz, other.rate_hz),
            max_latency_ms=_min_nonzero(self.max_latency_ms, other.max_latency_ms),
            max_jitter_ms=_min_nonzero(self.max_jitter_ms, other.max_jitter_ms),
            upstream_bps=_min_nonzero(self.upstream_bps, other.upstream_bps),
            downstream_bps=_min_nonzero(self.downstream_bps, other.downstream_bps),
            packet_size_bytes=_min_nonzero(self.packet_size_bytes, other.packet_size_bytes),
            reliability=rel,
            ordering=self.ordering and other.ordering,
            max_loss_pct=min(self.max_loss_pct, other.max_loss_pct)
            if (self.max_loss_pct and other.max_loss_pct)
            else max(self.max_loss_pct, other.max_loss_pct),
            priority=max(self.priority, other.priority),
        )

    def as_dict(self) -> dict:
        """Serializable form for negotiation payloads."""
        return {
            "rate_hz": self.rate_hz,
            "max_latency_ms": self.max_latency_ms,
            "max_jitter_ms": self.max_jitter_ms,
            "upstream_bps": self.upstream_bps,
            "downstream_bps": self.downstream_bps,
            "packet_size_bytes": self.packet_size_bytes,
            "reliability": self.reliability.value,
            "ordering": self.ordering,
            "max_loss_pct": self.max_loss_pct,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QoSDimensions":
        return cls(
            rate_hz=float(data.get("rate_hz", 0.0)),
            max_latency_ms=int(data.get("max_latency_ms", 0)),
            max_jitter_ms=int(data.get("max_jitter_ms", 0)),
            upstream_bps=int(data.get("upstream_bps", 0)),
            downstream_bps=int(data.get("downstream_bps", 0)),
            packet_size_bytes=int(data.get("packet_size_bytes", 0)),
            reliability=Reliability(data.get("reliability", "best_effort")),
            ordering=bool(data.get("ordering", True)),
            max_loss_pct=float(data.get("max_loss_pct", 0.0)),
            priority=int(data.get("priority", 0)),
        )


def _min_nonzero(a: float, b: float) -> float:
    """Take the smaller value, treating 0 (unconstrained) as +infinity."""
    if a == 0:
        return b
    if b == 0:
        return a
    return min(a, b)

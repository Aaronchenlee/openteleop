"""End-to-end QoS negotiation.

Both sides declare their QoS per topic; the adapter merges them by taking the
stricter value per dimension (rate/latency/jitter/bandwidth = min,
reliability = stricter). Failed negotiation (e.g. required bandwidth exceeds
what either side can provide) returns a structured failure the application can
retry with a downgraded declaration.
"""
from __future__ import annotations

from typing import Dict

from .qos import QoSDimensions, QoSError


class NegotiationResult:
    def __init__(
        self,
        ok: bool,
        agreed: Dict[str, QoSDimensions],
        error: str = "",
        code: str = "",
    ):
        self.ok = ok
        self.agreed = agreed
        self.error = error
        self.code = code

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "agreed": {t: q.as_dict() for t, q in self.agreed.items()},
            "error": self.error,
            "code": self.code,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NegotiationResult":
        return cls(
            ok=bool(data.get("ok")),
            agreed={
                t: QoSDimensions.from_dict(q)
                for t, q in data.get("agreed", {}).items()
            },
            error=data.get("error", ""),
            code=data.get("code", ""),
        )


def negotiate(
    local: Dict[str, QoSDimensions],
    remote: Dict[str, QoSDimensions],
) -> NegotiationResult:
    """Merge local and remote QoS declarations topic-by-topic (stricter wins).

    Topics present in only one side are dropped (both ends must agree on the
    topic set before any channel is created).
    """
    agreed: Dict[str, QoSDimensions] = {}
    for topic in local.keys() & remote.keys():
        try:
            agreed[topic] = local[topic].stricter(remote[topic])
        except QoSError as exc:  # pragma: no cover - defensive
            return NegotiationResult(
                False, agreed, str(exc), "qos_invalid"
            )
    return NegotiationResult(True, agreed)


def bandwidth_check(
    agreed: Dict[str, QoSDimensions],
    max_upstream_bps: int = 0,
    max_downstream_bps: int = 0,
) -> NegotiationResult:
    """Validate the merged table against network-side bandwidth limits.

    Returns a failed result if the aggregate demand exceeds the link budget,
    letting the application retry with a downgraded declaration.
    """
    up = sum(q.upstream_bps for q in agreed.values())
    down = sum(q.downstream_bps for q in agreed.values())
    if max_upstream_bps and up > max_upstream_bps:
        return NegotiationResult(
            False,
            agreed,
            f"aggregate upstream {up} bps exceeds budget {max_upstream_bps} bps",
            "bandwidth_exceeded",
        )
    if max_downstream_bps and down > max_downstream_bps:
        return NegotiationResult(
            False,
            agreed,
            f"aggregate downstream {down} bps exceeds budget {max_downstream_bps} bps",
            "bandwidth_exceeded",
        )
    return NegotiationResult(True, agreed)

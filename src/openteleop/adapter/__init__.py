"""QoS adapter layer: edge/cloud symmetric API with QoS negotiation, auth, monitoring."""

from .auth import (
    AuthError,
    SessionAuthorizer,
    SessionCredentials,
    generate_secret,
    hmac_sign,
    verify_handshake,
)
from .base import Adapter, Publisher, Subscription
from .channel_map import ChannelBinding, ChannelMap
from .cloud import CloudAdapter
from .edge import EdgeAdapter
from .monitor import QoSDegrader, QoSMetric, Violation
from .negotiate import NegotiationResult, bandwidth_check, negotiate
from .qos import QoSDimensions, QoSError, Reliability

__all__ = [
    "Adapter",
    "AuthError",
    "ChannelBinding",
    "ChannelMap",
    "CloudAdapter",
    "EdgeAdapter",
    "NegotiationResult",
    "Publisher",
    "QoSDegrader",
    "QoSDimensions",
    "QoSError",
    "QoSMetric",
    "Reliability",
    "SessionAuthorizer",
    "SessionCredentials",
    "Subscription",
    "Violation",
    "bandwidth_check",
    "generate_secret",
    "hmac_sign",
    "negotiate",
    "verify_handshake",
]

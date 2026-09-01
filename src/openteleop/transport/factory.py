"""Transport factory - picks the best transport for the deployment.

Selection logic:

* ``mode == LOCAL``        -> :class:`LocalZMQTransport`
* ``mode == REMOTE``       -> :class:`RemoteWebRTCTransport`
* ``mode == AUTO`` (default) -> ZMQ if the peer is reachable on the LAN,
  otherwise WebRTC.
"""

from __future__ import annotations

import socket
from typing import Optional

from ..config.settings import TeleopConfig, TransportMode
from .base import BaseTransport
from .local_zmq import LocalZMQTransport


def _peer_reachable(host: str, port: int, timeout_s: float = 0.5) -> bool:
    """Cheap TCP probe to decide ZMQ vs WebRTC for AUTO mode."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


class TransportFactory:
    """Creates the appropriate transport instance."""

    @staticmethod
    def create(
        config: TeleopConfig,
        *,
        role: str = "operator",
        peer_ip: Optional[str] = None,
        signaler: Optional[object] = None,
    ) -> BaseTransport:
        mode = config.mode
        if mode == TransportMode.AUTO:
            # Probe the robot's command channel port; if reachable -> ZMQ.
            if peer_ip is not None and _peer_reachable(peer_ip, 5555):
                mode = TransportMode.LOCAL
            else:
                mode = TransportMode.REMOTE

        if mode == TransportMode.LOCAL:
            return LocalZMQTransport(config, role=role, peer_ip=peer_ip or "127.0.0.1")
        if mode == TransportMode.REMOTE:
            from .remote_webrtc import RemoteWebRTCTransport

            return RemoteWebRTCTransport(config, role=role, signaler=signaler)
        raise ValueError(f"Unknown transport mode: {mode}")

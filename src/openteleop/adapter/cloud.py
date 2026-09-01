"""Cloud-side QoS adapter (operator / cloud side).

The Cloud adapter initiates the session: it sends the HMAC handshake, receives
a signed session credential, sends its QoS declarations, receives the merged
(table) result and the channel binding table, then wires its transport to match
the edge's bindings. It exposes the same ``advertise / subscribe / request``
API to operator-side applications.
"""
from __future__ import annotations

from typing import Dict, Optional

from ..config.settings import TeleopConfig
from ..transport.base import BaseTransport
from ..transport.factory import TransportFactory
from .auth import SessionCredentials, hmac_sign
from .base import Adapter
from .qos import QoSDimensions


class CloudAdapter(Adapter):
    """QoS adapter for the cloud (operator) side."""

    def __init__(
        self,
        config: TeleopConfig,
        transport: Optional[BaseTransport] = None,
    ):
        self._transport = transport or TransportFactory.create(
            config, role="operator", signaler=None
        )
        if transport is not None and getattr(transport, "role", "operator") != "operator":
            transport.role = "operator"
        self._cred: Optional[SessionCredentials] = None
        self._binding_table: Dict[str, dict] = {}
        super().__init__(self._transport, "operator")

    async def connect(self) -> None:
        self.bind_fixed_subscriptions()
        await self._transport.connect()

    async def close(self) -> None:
        await self.stop_monitor()
        await self._transport.close()

    # ---- session / negotiation ----
    async def handshake(self, secret: bytes, client_id: str) -> dict:
        """Send HMAC handshake to the edge and receive a signed credential."""
        ts = __import__("time").time()
        sig = hmac_sign(secret, client_id, ts)
        msg = {"type": "openteleop.handshake", "client_id": client_id, "ts": ts, "sig": sig}
        reply = await self._transport.send_state(msg)
        if not reply.get("ok"):
            return reply
        self._cred = SessionCredentials.from_dict(reply["cred"])
        self._session_id = self._cred.session_id
        return reply

    async def negotiate(self) -> dict:
        """Send this side's QoS declarations and receive the merged table."""
        msg = {
            "type": "openteleop.negotiate",
            "declarations": {t: q.as_dict() for t, q in self.local_declarations().items()},
        }
        reply = await self._transport.send_state(msg)
        if reply.get("ok"):
            agreed = {
                t: QoSDimensions.from_dict(q)
                for t, q in (reply.get("agreed") or {}).items()
            }
            self.apply_negotiated(agreed)
            # Adopt the edge's channel binding table (ports/owners must match).
            self._binding_table = reply.get("bindings") or {}
            self._adopt_bindings()
            # Join dynamic subscriptions now that bindings are final.
            await self.activate()
        return reply

    def _adopt_bindings(self) -> None:
        """Register the edge's binding table on this side's channel map."""
        from .channel_map import ChannelBinding

        for topic, b in self._binding_table.items():
            existing = self._channel_map.get(topic)
            binding = ChannelBinding(
                topic=topic,
                channel_name=b["channel"],
                port=int(b["port"]),
                owner=b["owner"],
                pattern="PUB",
                dynamic=True,
                qos=existing.qos if existing else QoSDimensions(),
            )
            self._channel_map.register(binding)
            if topic in self._publishers and binding.dynamic:
                self._transport.register_dynamic_channel(
                    binding.channel_name, binding.port, binding.owner
                )

    def credentials(self) -> Optional[SessionCredentials]:
        return self._cred

    def session_id(self) -> Optional[str]:
        return self._session_id

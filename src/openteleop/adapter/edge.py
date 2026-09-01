"""Edge-side QoS adapter (robot / device side).

The Edge adapter sits on the robot host. It:

* accepts the Cloud's HMAC handshake, validates it via :class:`SessionAuthorizer`,
  and issues a signed session credential (network-side auth);
* receives the Cloud's QoS declarations, merges them with its own
  (stricter-wins) via :func:`negotiate`, runs a bandwidth check, and returns
  the agreed table + channel binding table;
* exposes the symmetric ``advertise / subscribe / request`` API to robot-side
  applications.

Handshake and negotiation ride the reliable REQ/REP state channel, so the
authorization check is enforced *on the wire*, not just in-process.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from ..config.settings import TeleopConfig
from ..transport.base import BaseTransport
from ..transport.factory import TransportFactory
from .auth import SessionAuthorizer
from .base import Adapter
from .negotiate import bandwidth_check, negotiate
from .qos import QoSDimensions


class EdgeAdapter(Adapter):
    """QoS adapter for the edge (robot) side."""

    def __init__(
        self,
        config: TeleopConfig,
        transport: Optional[BaseTransport] = None,
        authorizer: Optional[SessionAuthorizer] = None,
    ):
        self._transport = transport or TransportFactory.create(
            config, role="robot", signaler=None
        )
        if transport is not None and getattr(transport, "role", "robot") != "robot":
            transport.role = "robot"
        self._authorizer = authorizer
        self._last_negotiation: Optional[dict] = None
        super().__init__(self._transport, "robot")

    async def connect(self) -> None:
        # Handle handshake + negotiation signals on the REQ/REP channel.
        self._transport.on_state(self._handle_signal)
        self.bind_fixed_subscriptions()
        await self._transport.connect()

    async def close(self) -> None:
        await self.stop_monitor()
        await self._transport.close()

    # ---- signal handling (called by transport REP) ----
    def _handle_signal(self, msg: dict) -> dict:
        stype = msg.get("type")
        if stype == "openteleop.handshake":
            return self._do_handshake(msg)
        if stype == "openteleop.negotiate":
            return self._do_negotiate(msg)
        return {"ok": False, "error": "unknown signal", "code": "bad_signal"}

    def _do_handshake(self, msg: dict) -> dict:
        if self._authorizer is None:
            return {
                "ok": False,
                "error": "authorizer not configured",
                "code": "no_authorizer",
            }
        client_id = msg.get("client_id", "")
        ts = float(msg.get("ts", 0))
        sig = msg.get("sig", "")
        try:
            cred = self._authorizer.authenticate(client_id, ts, sig)
            return {"ok": True, "cred": cred.to_dict()}
        except Exception as exc:  # AuthError
            return {"ok": False, "error": str(exc), "code": getattr(exc, "code", "auth_failed")}

    def _do_negotiate(self, msg: dict) -> dict:
        remote = {
            t: QoSDimensions.from_dict(q)
            for t, q in (msg.get("declarations") or {}).items()
        }
        local = self.local_declarations()
        result = negotiate(local, remote)
        if not result.ok:
            return result.as_dict()
        # Network bandwidth budget check (config-driven).
        max_up = getattr(self._transport.config, "link_upstream_bps", 0) or 0
        max_down = getattr(self._transport.config, "link_downstream_bps", 0) or 0
        budget = bandwidth_check(result.agreed, max_up, max_down)
        if not budget.ok:
            return budget.as_dict()
        self.apply_negotiated(result.agreed)
        # Activate any dynamic subscriptions this side owns (async join).
        asyncio.get_running_loop().create_task(self.activate())
        self._last_negotiation = {
            **result.as_dict(),
            "bindings": self._channel_map.to_negotiation_payload(),
        }
        return self._last_negotiation

    def last_negotiation(self) -> Optional[dict]:
        return self._last_negotiation

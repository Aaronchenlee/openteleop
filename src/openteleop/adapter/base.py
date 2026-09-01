"""Adapter base classes: Publisher, Subscription, Adapter.

The adapter layer exposes a symmetric application API on both edge and cloud:

* :meth:`Adapter.advertise` - create a typed publisher for a topic + QoS
* :meth:`Adapter.subscribe` - receive data for a topic
* :meth:`Adapter.request` - reliable request/ack (mode switches, config, e-stop)

An application only declares QoS (rate / latency / jitter / bandwidth /
packet size / reliability); the adapter maps that to a concrete channel
(fixed six or dynamic pool) and the underlying transport (ZMQ / WebRTC)
is invisible to the caller.
"""
from __future__ import annotations

import asyncio
import json
import pickle
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

from ..transport.base import BaseTransport
from .channel_map import ChannelBinding, ChannelMap
from .monitor import QoSDegrader
from .qos import QoSDimensions, Reliability


class Publisher:
    """Typed handle returned by :meth:`Adapter.advertise`."""

    def __init__(self, adapter: "Adapter", topic: str, qos: QoSDimensions):
        self._adapter = adapter
        self._topic = topic
        self._qos = qos

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def qos(self) -> QoSDimensions:
        return self._qos

    def publish(self, payload: bytes) -> None:
        """Publish raw bytes on this topic (fire-and-forget)."""
        self._adapter._publish(self._topic, payload)

    def publish_json(self, obj: Any) -> None:
        self.publish(json.dumps(obj, default=str).encode())

    def effective_qos(self) -> QoSDimensions:
        """QoS after any automatic degradation (for the app to observe)."""
        return self._adapter._degrader.effective(self._topic)


class Subscription:
    """Typed handle returned by :meth:`Adapter.subscribe`."""

    def __init__(self, adapter: "Adapter", topic: str, qos: QoSDimensions, callback: Callable):
        self._adapter = adapter
        self._topic = topic
        self._qos = qos
        self._callback = callback

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def qos(self) -> QoSDimensions:
        return self._qos

    def close(self) -> None:
        self._adapter._unsubscribe(self._topic)


class Adapter(ABC):
    """Shared adapter logic. Edge and Cloud subclasses wire the transport."""

    def __init__(self, transport: BaseTransport, role: str):
        self._transport = transport
        self._role = role
        self._channel_map = ChannelMap(transport.config)
        self._publishers: Dict[str, Publisher] = {}
        self._subscriptions: Dict[str, Subscription] = {}
        self._topic_cbs: Dict[str, Callable] = {}
        self._negotiated: Dict[str, QoSDimensions] = {}
        self._session_id: Optional[str] = None
        self._degrader: Optional[QoSDegrader] = None
        self._fixed_bound: set = set()
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    # ---- application API ----
    def advertise(self, topic: str, qos: QoSDimensions) -> Publisher:
        """Declare intent to publish ``topic`` with the given QoS."""
        if topic in self._publishers:
            return self._publishers[topic]
        self._authorize_publish(topic, qos)
        binding = self._channel_map.resolve(topic, qos, self._role)
        if binding.dynamic:
            self._transport.register_dynamic_channel(
                binding.channel_name, binding.port, binding.owner
            )
        pub = Publisher(self, topic, qos)
        self._publishers[topic] = pub
        return pub

    def subscribe(
        self, topic: str, qos: QoSDimensions, callback: Callable
    ) -> Subscription:
        """Subscribe to ``topic``; ``callback(payload_bytes, ts_us)``."""
        if topic in self._subscriptions:
            return self._subscriptions[topic]
        self._authorize_subscribe(topic, qos)
        binding = self._channel_map.resolve(topic, qos, self._role)
        if binding.dynamic:
            self._transport.register_dynamic_channel(
                binding.channel_name, binding.port, binding.owner
            )
        self._topic_cbs[topic] = callback
        sub = Subscription(self, topic, qos, callback)
        self._subscriptions[topic] = sub
        return sub

    async def request(self, topic: str, qos: QoSDimensions, payload: dict) -> dict:
        """Reliable request/ack (mode switch, config, e-stop)."""
        if qos.reliability != Reliability.RELIABLE:
            qos = QoSDimensions(**{**qos.as_dict(), "reliability": Reliability.RELIABLE})
        # Reliable requests ride the REQ/REP state channel regardless of topic.
        msg = {"topic": topic, "payload": payload}
        return await self._transport.send_state(msg)

    # ---- internals: binding ----
    def _bind_publish(self, binding: ChannelBinding) -> None:
        if binding.dynamic:
            self._transport.register_dynamic_channel(
                binding.channel_name, binding.port, binding.owner
            )

    def _make_dyn_cb(self, topic: str) -> Callable:
        def cb(payload: bytes, ts_us: int) -> None:
            c = self._topic_cbs.get(topic)
            if c:
                c(payload, ts_us)
        return cb

    def _activate(self, topic: str, binding: ChannelBinding) -> None:
        """(Re)bind a topic's transport path once its binding is final.

        Called after negotiation adopts the edge's binding table, so dynamic
        subscriptions join the channel at runtime (their SUB is created by
        ``transport.add_subscription``), and fixed-channel subscriptions wire
        the existing transport callbacks.
        """
        if binding.dynamic:
            self._transport.register_dynamic_channel(
                binding.channel_name, binding.port, binding.owner
            )
            self._transport._topic_cbs[topic.encode()] = self._make_dyn_cb(topic)
        else:
            self._bind_fixed_subscribe(binding.channel_name, topic)

    async def activate(self) -> None:
        """Activate all subscriptions against their final bindings.

        Run after negotiation. Fixed-channel callbacks are registered before
        ``connect()``; dynamic SUBs are joined at runtime.
        """
        for topic, sub in list(self._subscriptions.items()):
            binding = self._channel_map.get(topic)
            if binding is None:
                continue
            # Register the dispatch callback (topic-level), then join the channel.
            self._activate(topic, binding)
            if binding.dynamic:
                await self._transport.add_subscription(
                    topic.encode(), binding.channel_name
                )

    def _bind_fixed_subscribe(self, name: str, topic: str) -> None:
        if topic in self._fixed_bound:
            return
        self._fixed_bound.add(topic)
        if name == "state_reliable_back":
            def on_tel(d: dict) -> None:
                c = self._topic_cbs.get(topic)
                if c:
                    c(json.dumps(d, default=str).encode(), int(time.time() * 1e6))
            self._transport.on_telemetry(on_tel)
        elif name == "video":
            def on_vid(f: Any, ts_us: int) -> None:
                c = self._topic_cbs.get(topic)
                if c:
                    c(pickle.dumps(f), ts_us)
            self._transport.on_video_frame(on_vid)
        elif name == "tactile_unreliable":
            def on_tac(d: bytes, ts_us: int) -> None:
                c = self._topic_cbs.get(topic)
                if c:
                    c(d, ts_us)
            self._transport.on_tactile(on_tac)
        elif name == "cmd_unreliable":
            def on_cmd(a: bytes, ts_us: int, seq: int) -> None:
                c = self._topic_cbs.get(topic)
                if c:
                    c(a, ts_us)
            self._transport.on_command(on_cmd)

    # ---- internals: publish dispatch ----
    def _publish(self, topic: str, payload: bytes) -> None:
        binding = self._channel_map.get(topic)
        if binding is None:
            raise KeyError(f"topic '{topic}' not advertised")
        ts = int(time.time() * 1e6)
        if binding.dynamic:
            self._transport.publish_topic(binding.channel_name, topic.encode(), payload, ts)
        else:
            self._publish_fixed(binding, topic, payload, ts)

    def _publish_fixed(self, binding: ChannelBinding, topic: str, payload: bytes, ts: int) -> None:
        name = binding.channel_name
        if name == "cmd_unreliable":
            self._transport.send_command(payload, ts, 0)
        elif name == "state_reliable_back":
            self._transport.send_telemetry(json.loads(payload.decode()))
        elif name == "video":
            self._transport.send_video_frame(pickle.loads(payload), ts)
        elif name in ("tactile_unreliable", "audio"):
            self._transport.send_tactile(payload, ts)
        else:  # pragma: no cover
            raise KeyError(f"unsupported fixed channel {name}")

    def _unsubscribe(self, topic: str) -> None:
        self._subscriptions.pop(topic, None)
        self._topic_cbs.pop(topic, None)

    # ---- hooks for subclasses ----
    def _authorize_publish(self, topic: str, qos: QoSDimensions) -> None:
        pass

    def _authorize_subscribe(self, topic: str, qos: QoSDimensions) -> None:
        pass

    def bind_fixed_subscriptions(self) -> None:
        """Wire fixed-channel transport callbacks before connect()."""
        for topic, sub in list(self._subscriptions.items()):
            binding = self._channel_map.get(topic)
            if binding is not None and not binding.dynamic:
                self._bind_fixed_subscribe(binding.channel_name, topic)

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    # ---- negotiation integration ----
    def local_declarations(self) -> Dict[str, QoSDimensions]:
        """QoS declared by this side for all known topics.

        Both advertised (publish) and subscribed topics count: negotiation is
        about the *contract* both sides state for a topic, regardless of which
        direction each side flows data.
        """
        decl = {t: p.qos for t, p in self._publishers.items()}
        for t, s in self._subscriptions.items():
            decl.setdefault(t, s.qos)
        return decl

    def apply_negotiated(self, agreed: Dict[str, QoSDimensions]) -> None:
        """Store the merged QoS table and (re)build the degrader."""
        self._negotiated = agreed
        self._degrader = QoSDegrader(agreed)

    # ---- monitoring ----
    def start_monitor(self, interval_s: float = 0.5) -> None:
        """Start periodic QoS checking + auto-degradation."""
        if self._degrader is None:
            return
        self._running = True
        self._monitor_task = asyncio.get_running_loop().create_task(
            self._monitor_loop(interval_s)
        )

    async def stop_monitor(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _monitor_loop(self, interval_s: float) -> None:
        while self._running:
            metrics = self._collect_metrics()
            self._degrader.check(metrics)
            await asyncio.sleep(interval_s)

    def _collect_metrics(self) -> Dict[str, Any]:
        from .monitor import metrics_from_stats

        stats = self._transport.get_stats()
        out: Dict[str, Any] = {}
        for topic, binding in self._channel_map.bindings().items():
            ch_stats = stats.get(binding.channel_name)
            if ch_stats:
                m = metrics_from_stats(topic, ch_stats)
                if m:
                    out[topic] = m
        return out

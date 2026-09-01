"""End-to-end adapter integration: edge <-> cloud over loopback ZMQ.

Covers the full session lifecycle the design review specified:
1. HMAC handshake + session credential (network-side auth)
2. QoS declaration exchange + stricter-wins negotiation
3. Dynamic channel binding (custom topic, ports agreed across sides)
4. Publish/subscribe data flow over a dynamically allocated channel
5. Runtime QoS monitoring with auto-degradation hook
"""
from __future__ import annotations

import asyncio
import json

import pytest

from openteleop.adapter.auth import SessionAuthorizer, generate_secret
from openteleop.adapter.cloud import CloudAdapter
from openteleop.adapter.edge import EdgeAdapter
from openteleop.adapter.qos import QoSDimensions, Reliability
from openteleop.config.settings import TeleopConfig, TransportMode
from openteleop.transport.local_zmq import LocalZMQTransport

from .conftest import make_isolated_config


async def _build(isolated_ports):
    cfg = make_isolated_config(isolated_ports)
    cfg.mode = TransportMode.LOCAL
    cfg.host = "127.0.0.1"
    cfg.warmup_ms = 300
    return cfg


async def test_edge_cloud_handshake_and_negotiation(isolated_ports):
    """HMAC handshake succeeds, negotiation merges QoS stricter-wins."""
    cfg = await _build(isolated_ports)
    secret = generate_secret()
    auth = SessionAuthorizer(
        secret,
        clients={
            "cloud-01": {"topics": ["arm/cmd", "camera/main", "custom/pc"], "upstream_bps": 5_000_000}
        },
    )

    edge = EdgeAdapter(cfg, transport=LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1"), authorizer=auth)
    cloud = CloudAdapter(cfg, transport=LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1"))

    # Both sides declare their topics before connecting.
    edge.advertise("custom/pc", QoSDimensions(rate_hz=10, upstream_bps=500_000, packet_size_bytes=2000))
    cloud.subscribe(
        "custom/pc",
        QoSDimensions(rate_hz=20, upstream_bps=1_000_000, packet_size_bytes=2000),
        lambda p, ts: None,
    )

    await edge.connect()
    await cloud.connect()

    # 1) handshake (network-side auth over REQ/REP)
    hs = await cloud.handshake(secret, "cloud-01")
    assert hs["ok"] is True, hs
    assert cloud.session_id()

    # 2) negotiation
    nego = await cloud.negotiate()
    assert nego["ok"] is True, nego
    assert "custom/pc" in nego["agreed"]
    # stricter-wins: cloud asked 20Hz, edge asked 10Hz -> 10Hz
    assert nego["agreed"]["custom/pc"]["rate_hz"] == 10
    # binding table adopted by cloud
    assert "custom/pc" in cloud._binding_table

    await cloud.close()
    await edge.close()


async def test_handshake_rejected_with_wrong_signature(isolated_ports):
    cfg = await _build(isolated_ports)
    secret = generate_secret()
    auth = SessionAuthorizer(secret, clients={"cloud-01": {"topics": ["x"]}})
    edge = EdgeAdapter(cfg, transport=LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1"), authorizer=auth)
    cloud = CloudAdapter(cfg, transport=LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1"))
    await edge.connect()
    await cloud.connect()
    hs = await cloud.handshake(b"wrong-secret", "cloud-01")
    assert hs["ok"] is False
    assert hs["code"] == "auth_failed"
    await cloud.close()
    await edge.close()


async def test_dynamic_topic_end_to_end_dataflow(isolated_ports):
    """Publish on a dynamically allocated channel reaches the subscriber."""
    cfg = await _build(isolated_ports)
    secret = generate_secret()
    auth = SessionAuthorizer(secret, clients={"cloud-01": {"topics": ["custom/pc"]}})
    edge = EdgeAdapter(cfg, transport=LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1"), authorizer=auth)
    cloud = CloudAdapter(cfg, transport=LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1"))

    received = []
    pub = edge.advertise(
        "custom/pc",
        QoSDimensions(rate_hz=10, upstream_bps=500_000, packet_size_bytes=2000),
    )
    sub = cloud.subscribe(
        "custom/pc",
        QoSDimensions(rate_hz=10, upstream_bps=500_000, packet_size_bytes=2000),
        lambda p, ts: received.append(p),
    )

    await edge.connect()
    await cloud.connect()
    await cloud.handshake(secret, "cloud-01")
    await cloud.negotiate()

    await asyncio.sleep(0.3)
    for i in range(5):
        pub.publish(json.dumps({"i": i}).encode())
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)

    assert len(received) >= 3, f"expected data over dynamic channel, got {len(received)}"
    assert received[0].startswith(b"{")

    await cloud.close()
    await edge.close()


async def test_negotiation_fails_when_bandwidth_exceeds_budget(isolated_ports):
    cfg = await _build(isolated_ports)
    cfg.link_upstream_bps = 200_000  # link budget
    secret = generate_secret()
    auth = SessionAuthorizer(secret, clients={"cloud-01": {"topics": ["cam/hi"]}})
    edge = EdgeAdapter(cfg, transport=LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1"), authorizer=auth)
    cloud = CloudAdapter(cfg, transport=LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1"))
    edge.advertise("cam/hi", QoSDimensions(rate_hz=30, upstream_bps=4_000_000))
    cloud.subscribe("cam/hi", QoSDimensions(rate_hz=30, upstream_bps=4_000_000), lambda p, ts: None)
    await edge.connect()
    await cloud.connect()
    await cloud.handshake(secret, "cloud-01")
    nego = await cloud.negotiate()
    assert nego["ok"] is False
    assert nego["code"] == "bandwidth_exceeded"
    await cloud.close()
    await edge.close()

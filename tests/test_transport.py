"""Tests for the transport layer (ZMQ local transport)."""

from __future__ import annotations

import asyncio
import time

import pytest

from openteleop.transport.local_zmq import LocalZMQTransport

from .conftest import make_isolated_config


@pytest.fixture
def ports(isolated_ports):
    return isolated_ports


async def test_command_roundtrip_zmq(ports):
    """Operator sends a command; robot receives it with correct ts/seq."""
    rb_cfg = make_isolated_config(ports)
    op_cfg = make_isolated_config(ports)

    received = []
    # Robot side subscribes (SUB) to cmd_unreliable published by operator.
    rx = LocalZMQTransport(rb_cfg, role="robot", peer_ip="127.0.0.1")
    rx.on_command(lambda action, ts, seq: received.append((action, ts, seq)))
    await rx.connect()

    # Operator publishes cmd_unreliable.
    tx = LocalZMQTransport(op_cfg, role="operator", peer_ip="127.0.0.1")
    await tx.connect()

    # Wait for the subscriber to attach (slow-joiner avoidance).
    await asyncio.sleep(0.3)

    ts = int(time.time() * 1e6)
    tx.send_command(b"\x01\x02\x03", ts, 7)
    tx.send_command(b"\x04\x05\x06", ts + 1000, 8)

    await asyncio.sleep(0.3)
    assert len(received) >= 1, f"no commands received; pub errors: {[p.error for p in tx._publishers.values()]}"
    action, rts, seq = received[0]
    assert seq in (7, 8)
    assert rts == ts or rts == ts + 1000

    await tx.close()
    await rx.close()


async def test_state_ack_roundtrip(ports):
    """Reliable state command gets a whitelist check + ack (REQ/REP)."""
    rb_cfg = make_isolated_config(ports)
    op_cfg = make_isolated_config(ports)
    ack_seen = {}

    async def _run():
        # Robot binds REP and replies.
        rx = LocalZMQTransport(rb_cfg, role="robot", peer_ip="127.0.0.1")
        rx.on_state(lambda m: {"ok": True, "mode": m.get("payload", {}).get("mode")})
        await rx.connect()

        # Operator sends REQ.
        tx = LocalZMQTransport(op_cfg, role="operator", peer_ip="127.0.0.1")
        await tx.connect()
        await asyncio.sleep(0.2)
        ack = await tx.send_state({"type": "set_mode", "payload": {"mode": "teleop"}})
        ack_seen.update(ack)
        await tx.close()
        await rx.close()

    await _run()
    assert ack_seen.get("ok") is True
    assert ack_seen.get("mode") == "teleop"


async def test_telemetry_back_roundtrip(ports):
    """Robot publishes telemetry; operator receives it."""
    rb_cfg = make_isolated_config(ports)
    op_cfg = make_isolated_config(ports)
    telemetry_seen = []

    rx = LocalZMQTransport(op_cfg, role="operator", peer_ip="127.0.0.1")
    rx.on_telemetry(lambda t: telemetry_seen.append(t))
    await rx.connect()

    tx = LocalZMQTransport(rb_cfg, role="robot", peer_ip="127.0.0.1")
    await tx.connect()
    await asyncio.sleep(0.3)
    tx.send_telemetry({"mode": "teleop", "estopped": False})

    await asyncio.sleep(0.3)
    assert telemetry_seen, f"no telemetry received; pub errors: {[p.error for p in tx._publishers.values()]}"
    assert telemetry_seen[0]["mode"] == "teleop"

    await tx.close()
    await rx.close()


async def test_stats_tracked(ports):
    """Channel stats accumulate after commands flow."""
    rb_cfg = make_isolated_config(ports)
    op_cfg = make_isolated_config(ports)

    rx = LocalZMQTransport(rb_cfg, role="robot", peer_ip="127.0.0.1")
    rx.on_command(lambda a, t, s: None)
    await rx.connect()

    tx = LocalZMQTransport(op_cfg, role="operator", peer_ip="127.0.0.1")
    await tx.connect()
    await asyncio.sleep(0.3)
    for i in range(20):
        tx.send_command(b"\x00" * 4, int(time.time() * 1e6) + i * 1000, i)
    await asyncio.sleep(0.3)
    stats = rx.get_stats()
    assert stats["cmd_unreliable"]["rate_hz"] > 0
    await tx.close()
    await rx.close()

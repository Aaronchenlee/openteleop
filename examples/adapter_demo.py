"""Example: QoS adapter layer - edge <-> cloud session with auth + negotiation.

Run:
    python examples/adapter_demo.py

Shows the full adapter lifecycle on one host (loopback ZMQ):
1. Edge and Cloud declare topics with 8-dimension QoS
2. HMAC handshake over the reliable state channel (network-side auth)
3. QoS negotiation (stricter-wins) + channel binding exchange
4. Publish/subscribe data flow over a dynamically allocated channel
5. Runtime QoS monitoring with auto-degradation
"""
from __future__ import annotations

import asyncio
import json
import time

from openteleop.adapter.auth import SessionAuthorizer, generate_secret
from openteleop.adapter.cloud import CloudAdapter
from openteleop.adapter.edge import EdgeAdapter
from openteleop.adapter.qos import QoSDimensions, Reliability
from openteleop.config.settings import TeleopConfig, TransportMode
from openteleop.transport.local_zmq import LocalZMQTransport


async def main() -> None:
    cfg = TeleopConfig.default_robot()
    cfg.mode = TransportMode.LOCAL
    cfg.host = "127.0.0.1"
    cfg.warmup_ms = 300

    # Network-side auth: shared secret + per-client ACL/quotas.
    secret = generate_secret()
    authorizer = SessionAuthorizer(
        secret,
        clients={
            "cloud-01": {
                "topics": ["custom/pc", "arm/cmd"],
                "upstream_bps": 2_000_000,
            }
        },
    )

    edge = EdgeAdapter(
        cfg,
        transport=LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1"),
        authorizer=authorizer,
    )
    cloud = CloudAdapter(
        cfg,
        transport=LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1"),
    )

    # ---- edge declares a custom dynamic topic (point cloud, 10 Hz, 500 Kbps) ----
    pc_pub = edge.advertise(
        "custom/pc",
        QoSDimensions(rate_hz=10, upstream_bps=500_000, packet_size_bytes=2000),
    )

    # ---- cloud declares the same topic (asks 20 Hz) + a reliable command ----
    received = []

    def on_pc(payload: bytes, ts_us: int) -> None:
        received.append(payload)

    cloud.subscribe(
        "custom/pc",
        QoSDimensions(rate_hz=20, upstream_bps=1_000_000, packet_size_bytes=2000),
        on_pc,
    )

    await edge.connect()
    await cloud.connect()

    # 1) handshake (HMAC over REQ/REP - enforced on the wire)
    hs = await cloud.handshake(secret, "cloud-01")
    print(f"[auth] handshake ok={hs['ok']} session={cloud.session_id()[:8]}...")

    # 2) negotiation - stricter wins (edge 10Hz vs cloud 20Hz -> 10Hz)
    nego = await cloud.negotiate()
    agreed = nego.get("agreed", {})
    print(
        f"[negotiate] ok={nego['ok']} topic='custom/pc' "
        f"agreed_rate={agreed.get('custom/pc', {}).get('rate_hz')}Hz "
        f"agreed_upstream={agreed.get('custom/pc', {}).get('upstream_bps')}bps"
    )

    # 3) publish over the dynamically allocated channel
    for i in range(10):
        pc_pub.publish(json.dumps({"seq": i, "pts": 128}).encode())
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    print(f"[data] received {len(received)} point-cloud messages over dynamic channel")

    # 4) monitoring + auto-degradation
    cloud.apply_negotiated(agreed)
    cloud.start_monitor(interval_s=0.2)
    await asyncio.sleep(0.4)
    await cloud.stop_monitor()
    print("[monitor] started/stopped; degradation ladder is active")

    await cloud.close()
    await edge.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())

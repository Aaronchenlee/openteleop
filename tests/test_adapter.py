"""Tests for QoS dimensions, negotiation, channel map, auth, monitor."""

from __future__ import annotations

import time

import pytest

from openteleop.adapter.auth import (
    AuthError,
    SessionAuthorizer,
    generate_secret,
    hmac_sign,
    verify_handshake,
)
from openteleop.adapter.channel_map import ChannelMap
from openteleop.adapter.monitor import QoSDegrader, QoSMetric
from openteleop.adapter.negotiate import bandwidth_check, negotiate
from openteleop.adapter.qos import QoSDimensions, QoSError, Reliability
from openteleop.config.settings import TeleopConfig


# ---- QoSDimensions ----
def test_qos_validation_rejects_negative_rate():
    with pytest.raises(QoSError):
        QoSDimensions(rate_hz=-1)


def test_qos_validation_rejects_reliable_with_loss():
    with pytest.raises(QoSError):
        QoSDimensions(reliability=Reliability.RELIABLE, max_loss_pct=1.0)


def test_qos_stricter_takes_min_and_higher_reliability():
    a = QoSDimensions(rate_hz=500, max_latency_ms=10, reliability=Reliability.BEST_EFFORT)
    b = QoSDimensions(rate_hz=200, max_latency_ms=5, reliability=Reliability.RELIABLE)
    m = a.stricter(b)
    assert m.rate_hz == 200
    assert m.max_latency_ms == 5
    assert m.reliability == Reliability.RELIABLE


def test_qos_stricter_treats_zero_as_unconstrained():
    a = QoSDimensions(rate_hz=0, max_latency_ms=20)
    b = QoSDimensions(rate_hz=100, max_latency_ms=0)
    m = a.stricter(b)
    assert m.rate_hz == 100
    assert m.max_latency_ms == 20


def test_qos_roundtrip_dict():
    q = QoSDimensions(rate_hz=30, upstream_bps=4_000_000, reliability=Reliability.PARTIAL)
    assert QoSDimensions.from_dict(q.as_dict()) == q


# ---- negotiation ----
def test_negotiate_intersection_and_stricter():
    local = {"video": QoSDimensions(rate_hz=30, upstream_bps=4_000_000)}
    remote = {
        "video": QoSDimensions(rate_hz=60, upstream_bps=2_000_000),
        "extra": QoSDimensions(rate_hz=10),
    }
    r = negotiate(local, remote)
    assert r.ok
    assert "video" in r.agreed
    assert "extra" not in r.agreed  # topics must exist on both sides
    assert r.agreed["video"].rate_hz == 30
    assert r.agreed["video"].upstream_bps == 2_000_000


def test_bandwidth_check_fails_when_over_budget():
    agreed = {"video": QoSDimensions(rate_hz=30, upstream_bps=4_000_000)}
    ok = bandwidth_check(agreed, max_upstream_bps=2_000_000)
    assert not ok.ok
    assert ok.code == "bandwidth_exceeded"


# ---- channel map ----
def test_channel_map_maps_reliable_to_state():
    cfg = TeleopConfig.default_robot()
    cm = ChannelMap(cfg)
    b = cm.resolve("config/mode", QoSDimensions(reliability=Reliability.RELIABLE), "robot")
    assert b.channel_name == "state_reliable"
    assert not b.dynamic


def test_channel_map_maps_video_to_video():
    cfg = TeleopConfig.default_robot()
    cm = ChannelMap(cfg)
    b = cm.resolve("camera/main", QoSDimensions(rate_hz=30, upstream_bps=4_000_000), "robot")
    assert b.channel_name == "video"


def test_channel_map_allocates_dynamic_for_custom_topic():
    cfg = TeleopConfig.default_robot()
    cm = ChannelMap(cfg)
    b = cm.resolve(
        "custom/pointcloud",
        QoSDimensions(rate_hz=10, upstream_bps=500_000, packet_size_bytes=2000),
        "robot",
    )
    assert b.dynamic
    assert b.port not in {c.port for c in cfg.channels}
    # upstream-heavy -> robot binds
    assert b.owner == "robot"


def test_channel_map_dynamic_downstream_bound_by_operator():
    cfg = TeleopConfig.default_robot()
    cm = ChannelMap(cfg)
    b = cm.resolve(
        "custom/cmd2",
        QoSDimensions(rate_hz=100, downstream_bps=100_000),
        "operator",
    )
    assert b.dynamic
    assert b.owner == "operator"


# ---- auth ----
def test_hmac_sign_verify():
    secret = generate_secret()
    ts = time.time()
    sig = hmac_sign(secret, "alice", ts)
    assert verify_handshake(secret, "alice", ts, sig)
    assert not verify_handshake(secret, "alice", ts + 999, sig)  # stale
    assert not verify_handshake(secret, "mallory", ts, sig)


def test_authorizer_issues_credentials_and_enforces_acl():
    secret = generate_secret()
    auth = SessionAuthorizer(
        secret,
        clients={
            "alice": {"topics": ["arm/cmd", "arm/state"], "upstream_bps": 1_000_000}
        },
    )
    ts = time.time()
    cred = auth.authenticate("alice", ts, hmac_sign(secret, "alice", ts))
    assert cred.session_id
    auth.check_publish(cred, "arm/cmd")  # allowed
    with pytest.raises(AuthError) as e:
        auth.check_publish(cred, "forbidden/topic")
    assert e.value.code == "topic_forbidden"


def test_authorizer_quota():
    secret = generate_secret()
    auth = SessionAuthorizer(
        secret,
        clients={"bob": {"topics": [], "upstream_bps": 100_000}},
    )
    ts = time.time()
    cred = auth.authenticate("bob", ts, hmac_sign(secret, "bob", ts))
    auth.check_bandwidth(cred, upstream_bps=50_000, downstream_bps=0)
    with pytest.raises(AuthError) as e:
        auth.check_bandwidth(cred, upstream_bps=200_000, downstream_bps=0)
    assert e.value.code == "quota_exceeded"


def test_authorizer_rejects_bad_signature():
    secret = generate_secret()
    auth = SessionAuthorizer(secret, clients={"alice": {}})
    ts = time.time()
    with pytest.raises(AuthError):
        auth.authenticate("alice", ts, "deadbeef")


# ---- monitor ----
def test_monitor_detects_rate_violation_and_degrades():
    expected = {"cmd": QoSDimensions(rate_hz=500)}
    deg = QoSDegrader(expected, auto_degrade=True)
    m = QoSMetric(rate_hz=100, sample_count=10)
    violations = deg.check({"cmd": m})
    assert any(v.dimension == "rate" for v in violations)
    # degraded: reliability upgraded first
    assert deg.state("cmd").reliability == Reliability.PARTIAL


def test_monitor_ladder_goes_down_to_reliable_then_rate():
    expected = {"cmd": QoSDimensions(rate_hz=500)}
    deg = QoSDegrader(expected, auto_degrade=True)
    m = QoSMetric(rate_hz=10, sample_count=10)
    for _ in range(10):
        deg.check({"cmd": m})
    st = deg.state("cmd")
    assert st.reliability == Reliability.RELIABLE
    assert st.rate_idx > 0
    eff = deg.effective("cmd")
    assert eff.rate_hz < 500


def test_monitor_recovers_stepwise():
    expected = {"cmd": QoSDimensions(rate_hz=500)}
    deg = QoSDegrader(expected, auto_degrade=True)
    bad = QoSMetric(rate_hz=10, sample_count=10)
    good = QoSMetric(rate_hz=500, sample_count=10)
    for _ in range(3):
        deg.check({"cmd": bad})
    assert deg.state("cmd").active()
    for _ in range(5):
        deg.check({"cmd": good})
    assert not deg.state("cmd").active()


def test_monitor_latency_violation():
    expected = {"v": QoSDimensions(max_latency_ms=20)}
    deg = QoSDegrader(expected, auto_degrade=True)
    m = QoSMetric(latency_ms=80, rate_hz=30, sample_count=5)
    vs = deg.check({"v": m})
    assert any(v.dimension == "latency" for v in vs)

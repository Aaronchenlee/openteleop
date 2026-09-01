# QoS Adapter Layer

The adapter layer gives **robot-side (edge) and operator-side (cloud) applications**
a symmetric, QoS-first publish/subscribe API, and turns each application's
declared QoS contract into a concrete transport binding — mapping onto the fixed
six channels when semantics match, or allocating a **dynamic channel** from a
pool when they don't. It also adds network-side authentication, bandwidth
budgeting, and runtime monitoring with automatic degradation.

```
            EDGE (robot)                                  CLOUD (operator)
┌─────────────────────────────┐              ┌──────────────────────────────┐
│  application code           │              │  application code            │
│  advertise(topic, qos)      │              │  subscribe(topic, qos, cb)   │
│  subscribe(topic, qos, cb)  │              │  advertise(topic, qos)       │
│  request(...)               │              │  request(...)                │
│            │                │              │             │                │
│  ┌─────────▼──────────┐     │              │  ┌──────────▼─────────┐      │
│  │    EdgeAdapter     │     │              │  │    CloudAdapter    │      │
│  │  local declarations│     │  handshake    │  │  QoS declarations  │      │
│  │  + HMAC verify     │◄────┼──────────────┼──┤  HMAC sign         │      │
│  │  + stricter merge  │     │  negotiation  │  │  + receive cred    │      │
│  │  + bandwidth check │◄────┼──────────────┼──┤  + adopt bindings  │      │
│  └─────────┬──────────┘     │              │  └──────────┬─────────┘      │
│            ▼                │              │             ▼                │
│  ChannelMap: fixed 6 +      │              │  ChannelMap (mirrored)       │
│  dynamic pool (6100+)       │              │                              │
│            │                │              │             │                │
│  ┌─────────▼──────────┐     │              │  ┌──────────▼─────────┐      │
│  │  LocalZMQTransport │◄────┼── six fixed ─┼──┤  LocalZMQTransport │      │
│  │  (dynamic topics)  │     │  + dynamic    │  │  (dynamic topics)  │      │
│  └────────────────────┘     │              │  └────────────────────┘      │
└─────────────────────────────┘              └──────────────────────────────┘
```

## API (symmetric on both sides)

```python
from openteleop.adapter import QoSDimensions, Reliability

# Robot side
edge = EdgeAdapter(cfg, authorizer=authorizer)
pub = edge.advertise(
    "custom/pointcloud",
    QoSDimensions(rate_hz=10, upstream_bps=500_000, packet_size_bytes=2000),
)
pub.publish(payload_bytes)

# Operator side
cloud = CloudAdapter(cfg)
cloud.subscribe(
    "custom/pointcloud",
    QoSDimensions(rate_hz=20, upstream_bps=1_000_000, packet_size_bytes=2000),
    lambda payload, ts_us: handle(payload),
)

await edge.connect()
await cloud.connect()
await cloud.handshake(secret, "cloud-01")   # 1. HMAC auth
await cloud.negotiate()                      # 2. stricter-wins QoS merge
```

## QoS dimensions

| Field | Meaning | Default |
|---|---|---|
| `rate_hz` | publisher sample rate (Hz) | 0 = unconstrained |
| `max_latency_ms` | one-way latency budget (ms) | 0 = unconstrained |
| `max_jitter_ms` | latency jitter budget (ms) | 0 = unconstrained |
| `upstream_bps` | edge→cloud bandwidth budget (bps) | 0 = unconstrained |
| `downstream_bps` | cloud→edge bandwidth budget (bps) | 0 = unconstrained |
| `packet_size_bytes` | expected payload size (bytes) | 0 = unconstrained |
| `reliability` | `BEST_EFFORT` / `PARTIAL` / `RELIABLE` | `BEST_EFFORT` |
| `ordering` | strict ordering required | True |
| `max_loss_pct` | tolerable loss (%; must be 0 for RELIABLE) | 0.0 |
| `priority` | relative priority hint | 0 |

`validate()` rejects `RELIABLE` with a non-zero `max_loss_pct`.
`stricter()` merges two declarations taking the **stricter of each dimension**
(min of rate/latency/jitter/bandwidth, higher reliability).

## Negotiation

1. Both sides call `advertise` / `subscribe` with their declarations **before** `connect`.
2. Cloud sends its declarations over the reliable REQ/REP state channel.
3. Edge merges with its own (`negotiate`): only topics declared on **both** sides
   survive; per-topic QoS is the **stricter** of the two.
4. `bandwidth_check` verifies aggregate up/down bandwidth against the configured
   link budget (`link_upstream_bps` / `link_downstream_bps`); the session is
   rejected with `bandwidth_exceeded` if over.
5. Edge returns the agreed table **plus its channel binding table** (channel name,
   port, owner for every topic). Cloud adopts it, and dynamic subscriptions join
   their channels at runtime via `transport.add_subscription`.

## Channel mapping

`ChannelMap.resolve(topic, qos, role)` is **conservative**: only unambiguous
semantics reuse the fixed six channels — `RELIABLE` rides `state_reliable`
(REQ/REP), video-grade upstream (≥1 Mbps) rides `video`. Everything else gets a
**dynamic channel** (`dyn_<topic>`), allocated from port 6100 upward. The
`owner` (who binds) is chosen by data direction: upstream-heavy topics bind on
the robot, downstream-heavy on the operator.

## Authentication & authorization (network-side)

| Layer | Mechanism |
|---|---|
| Connection | HMAC-SHA256 handshake over the reliable channel (`hmac_sign` / `verify_handshake`, 60 s TTL against replay) |
| Topic ACL | signed `SessionCredentials` carry `allowed_topics`; `check_publish` / `check_subscribe` reject unknown topics (`topic_forbidden`) |
| Bandwidth quota | per-client `upstream_bps` / `downstream_bps`; `check_bandwidth` rejects over-quota sessions (`quota_exceeded`) |

## Monitoring & degradation

`QoSDegrader` samples runtime metrics per topic (`QoSMetric`): rate, latency,
jitter, loss, bandwidth. When a topic violates its **effective** (post-degradation)
contract, it walks a degradation ladder:

1. Reliability: `BEST_EFFORT → PARTIAL → RELIABLE` (upgrade transport effort)
2. Rate: `500 → 200 → 100 → 50 → 20 → 10 Hz`
3. Bitrate: `0.8 → 0.6 → 0.4 → 0.25 → 0.15 → 0.1 ×`
4. Packet size: same multiplicative ladder

Recovery is stepwise (one rung per healthy interval) to avoid oscillation.
`metrics_from_stats` adapts `ChannelStats` into `QoSMetric`, so existing
transport counters feed the monitor directly.

## Configuration

```python
cfg.link_upstream_bps = 2_000_000    # network-side budget (0 = unlimited)
cfg.link_downstream_bps = 2_000_000
cfg.warmup_ms = 300                  # publisher settle window
```

## Files

| File | Contents |
|---|---|
| `adapter/qos.py` | `QoSDimensions`, `Reliability`, validation, stricter merge |
| `adapter/channel_map.py` | fixed six + dynamic channel pool, owner assignment |
| `adapter/auth.py` | HMAC handshake, `SessionAuthorizer`, ACL + quotas |
| `adapter/negotiate.py` | negotiation + bandwidth check |
| `adapter/monitor.py` | `QoSMetric`, `Violation`, `QoSDegrader` ladder |
| `adapter/base.py` | `Adapter` / `Publisher` / `Subscription` abstraction |
| `adapter/edge.py` / `cloud.py` | concrete side implementations |

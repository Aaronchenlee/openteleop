"""ZMQ local transport - lowest latency for LAN deployments.

Design follows BEAVR's network module:

* one dedicated background thread per (host, port) publisher
* PUB/SUB with HWM (high-water mark) - when full, the *oldest* message is
  dropped, because a stale control command is worse than no command
* REQ/REP handshake for reliable commands (solves the slow-joiner problem)

**Port ownership.** Each logical channel is bound by exactly one side and
connected by the other, so the two roles can coexist on one host:

===============  ===========  ============  ===========
channel          owner (bind)  pattern      other side
===============  ===========  ============  ===========
cmd_unreliable   operator     PUB          robot SUB (connect)
state_reliable   robot        REP          operator REQ (connect)
state_reliable_back robot     PUB          operator SUB (connect)
video            robot        PUB          operator SUB (connect)
tactile_unreliable robot      PUB          operator SUB (connect)
audio            robot        PUB          operator SUB (connect)
===============  ===========  ============  ===========

Callbacks may be registered before :meth:`connect`; sockets are actually
created inside :meth:`connect` so the receiver pump never starts before the
transport is running.
"""

from __future__ import annotations

import asyncio
import json
import pickle
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import zmq
import zmq.asyncio

from ..config.settings import TeleopConfig
from .base import BaseTransport

DEFAULT_HWM = 10

# Which role binds each channel (the other role connects).
BIND_OWNER = {
    "cmd_unreliable": "operator",
    "state_reliable": "robot",
    "state_reliable_back": "robot",
    "video": "robot",
    "tactile_unreliable": "robot",
    "audio": "robot",
}

DEFAULT_PORTS = {
    "cmd_unreliable": 5555,
    "state_reliable": 5556,
    "state_reliable_back": 5557,
    "video": 5558,
    "tactile_unreliable": 5559,
    "audio": 5560,
}


def _channel_port(config: TeleopConfig, name: str) -> int:
    chan = next((c for c in config.channels if c.name == name), None)
    return chan.port if chan else DEFAULT_PORTS[name]


class _PublisherThread:
    """One background thread owning one ZMQ PUB socket (BEAVR ZMQPublisherManager)."""

    def __init__(self, address: str, hwm: int):
        self._address = address
        self._hwm = hwm
        self._queue: "queue.Queue[tuple[bytes, bytes]]" = queue.Queue(maxsize=hwm)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ctx = zmq.Context.instance()
        self._error: Optional[str] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="zmq-pub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def publish(self, topic: bytes, payload: bytes) -> None:
        try:
            self._queue.put_nowait((topic, payload))
        except queue.Full:
            # Drop the *oldest* pending message instead of the newest.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((topic, payload))
            except Exception:
                pass

    @property
    def error(self) -> Optional[str]:
        return self._error

    def _run(self) -> None:
        socket = self._ctx.socket(zmq.PUB)
        socket.set_hwm(self._hwm)
        try:
            socket.bind(self._address)
        except zmq.ZMQError as exc:
            self._error = f"ZMQ bind failed on {self._address}: {exc}"
            return
        try:
            while self._running:
                try:
                    topic, payload = self._queue.get(timeout=0.05)
                    socket.send_multipart([topic, payload])
                except queue.Empty:
                    continue
                except zmq.ZMQError:
                    break
        finally:
            socket.close(linger=0)


class LocalZMQTransport(BaseTransport):
    """ZMQ-based transport for low-latency LAN teleoperation."""

    def __init__(self, config: TeleopConfig, role: str = "operator", peer_ip: str = "127.0.0.1"):
        super().__init__(config)
        self.role = role
        self._peer_ip = peer_ip
        # Use a per-instance asyncio context (NOT Context.instance()): the
        # singleton binds the first event loop it touches, which breaks under
        # pytest-asyncio where each test runs on its own loop.
        self._ctx = zmq.asyncio.Context()
        self._publishers: Dict[str, _PublisherThread] = {}
        self._sub_sockets: list[Any] = []
        self._sub_tasks: list[asyncio.Task] = []
        self._rep_socket: Optional[Any] = None
        self._rep_task: Optional[asyncio.Task] = None
        self._pending_subs: List[Tuple[bytes, str]] = []
        self._need_rep = False

        self._cmd_cb: Optional[Callable] = None
        self._state_cb: Optional[Callable] = None
        self._telemetry_cb: Optional[Callable] = None
        self._video_cb: Optional[Callable] = None
        self._tactile_cb: Optional[Callable] = None

    # ---- address helpers ----
    def _publisher_addr(self, name: str) -> str:
        port = _channel_port(self.config, name)
        if BIND_OWNER[name] == self.role:
            return f"tcp://{self.config.host}:{port}"
        return f"tcp://{self._peer_ip}:{port}"

    def _publisher_for(self, name: str) -> _PublisherThread:
        addr = self._publisher_addr(name)
        if addr not in self._publishers:
            chan = next((c for c in self.config.channels if c.name == name), None)
            hwm = chan.hwm if chan else DEFAULT_HWM
            pub = _PublisherThread(addr, hwm)
            pub.start()
            self._publishers[addr] = pub
        return self._publishers[addr]

    # ---- cmd_unreliable ----
    def send_command(self, action: bytes, timestamp_us: int, seq: int) -> None:
        payload = timestamp_us.to_bytes(8, "little") + bytes([seq]) + action
        self._publisher_for("cmd_unreliable").publish(b"cmd", payload)

    def on_command(self, callback: Callable) -> None:
        self._cmd_cb = callback
        self._pending_subs.append((b"cmd", "cmd_unreliable"))

    # ---- state_reliable (REQ/REP, robot binds) ----
    async def send_state(self, message: dict, timeout_s: float = 2.0) -> dict:
        port = _channel_port(self.config, "state_reliable")
        req = self._ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, int(timeout_s * 1000))
        req.connect(f"tcp://{self._peer_ip}:{port}")
        try:
            await req.send(json.dumps(message).encode())
            reply = await req.recv()
            return json.loads(reply.decode())
        except zmq.Again:
            return {"ok": False, "error": "timeout", "nonce": message.get("nonce")}
        finally:
            req.close(linger=0)

    def on_state(self, callback: Callable) -> None:
        self._state_cb = callback
        if self.role == "robot":
            self._need_rep = True

    async def _rep_loop(self) -> None:
        while self._running:
            try:
                msg = await self._rep_socket.recv()
                message = json.loads(msg.decode())
                ack = self._state_cb(message) if self._state_cb else {"ok": True}
                ack.setdefault("nonce", message.get("nonce"))
                await self._rep_socket.send(json.dumps(ack).encode())
            except (zmq.ZMQError, asyncio.CancelledError):
                break

    # ---- state_reliable_back (robot PUB -> operator SUB) ----
    def send_telemetry(self, telemetry: dict) -> None:
        self._publisher_for("state_reliable_back").publish(
            b"telemetry", json.dumps(telemetry).encode()
        )

    def on_telemetry(self, callback: Callable) -> None:
        self._telemetry_cb = callback
        self._pending_subs.append((b"telemetry", "state_reliable_back"))

    # ---- video (robot PUB -> operator SUB) ----
    def send_video_frame(self, frame: Any, timestamp_us: int) -> None:
        payload = timestamp_us.to_bytes(8, "little") + pickle.dumps(frame)
        self._publisher_for("video").publish(b"video", payload)

    def on_video_frame(self, callback: Callable) -> None:
        self._video_cb = callback
        self._pending_subs.append((b"video", "video"))

    # ---- tactile (robot PUB -> operator SUB) ----
    def send_tactile(self, data: bytes, timestamp_us: int) -> None:
        payload = timestamp_us.to_bytes(8, "little") + data
        self._publisher_for("tactile_unreliable").publish(b"tactile", payload)

    def on_tactile(self, callback: Callable) -> None:
        self._tactile_cb = callback
        self._pending_subs.append((b"tactile", "tactile_unreliable"))

    # ---- subscription machinery ----
    def _ensure_publishers(self) -> None:
        """Pre-bind every publisher owned by this role.

        Critical for the ZMQ slow-joiner problem: a SUB that connects before
        the PUB exists drops the earliest messages. By binding our publishers
        inside connect() (before subscriptions are created), the peer's SUB
        always connects to an already-listening PUB.
        """
        for name in BIND_OWNER:
            # state_reliable is REP (not PUB) and is bound in _start_subscriptions.
            if name != "state_reliable" and BIND_OWNER[name] == self.role:
                self._publisher_for(name)

    def _start_subscriptions(self) -> None:
        loop = asyncio.get_running_loop()
        for topic, channel_name in self._pending_subs:
            port = _channel_port(self.config, channel_name)
            sub = self._ctx.socket(zmq.SUB)
            sub.setsockopt(zmq.RCVTIMEO, 100)
            sub.connect(f"tcp://{self._peer_ip}:{port}")
            sub.subscribe(topic)
            self._sub_sockets.append(sub)

            async def _pump(sub=sub, topic=topic) -> None:
                while self._running:
                    try:
                        msg = await sub.recv_multipart()
                        self._dispatch(topic, msg)
                    except zmq.Again:
                        continue
                    except (zmq.ZMQError, asyncio.CancelledError):
                        break

            self._sub_tasks.append(loop.create_task(_pump()))
        self._pending_subs.clear()

        if self._need_rep and self.role == "robot":
            self._rep_socket = self._ctx.socket(zmq.REP)
            self._rep_socket.bind(
                f"tcp://{self.config.host}:{_channel_port(self.config, 'state_reliable')}"
            )
            self._rep_task = loop.create_task(self._rep_loop())

    def _dispatch(self, topic: bytes, msg: list[bytes]) -> None:
        if topic == b"cmd":
            raw = msg[1]
            ts = int.from_bytes(raw[:8], "little")
            seq = raw[8]
            action = raw[9:]
            if self._cmd_cb:
                self._cmd_cb(action, ts, seq)
            self._stats["cmd_unreliable"].observe(ts, int(time.time() * 1e6))
        elif topic == b"telemetry":
            if self._telemetry_cb:
                self._telemetry_cb(json.loads(msg[1].decode()))
        elif topic == b"video":
            raw = msg[1]
            ts = int.from_bytes(raw[:8], "little")
            frame = pickle.loads(raw[8:])
            if self._video_cb:
                self._video_cb(frame, ts)
            self._stats["video"].observe(ts, int(time.time() * 1e6))
        elif topic == b"tactile":
            raw = msg[1]
            ts = int.from_bytes(raw[:8], "little")
            if self._tactile_cb:
                self._tactile_cb(raw[8:], ts)
            self._stats["tactile_unreliable"].observe(ts, int(time.time() * 1e6))

    # ---- lifecycle ----
    async def connect(self) -> None:
        self._running = True
        self._ensure_publishers()
        # Settle window for slow-joiner subscribers on the peer side.
        warmup = getattr(self.config, "warmup_ms", 0) / 1000.0
        if warmup > 0:
            await asyncio.sleep(warmup)
        self._start_subscriptions()

    async def close(self) -> None:
        self._running = False
        for pub in self._publishers.values():
            pub.stop()
        for sub in self._sub_sockets:
            sub.close(linger=0)
        for task in self._sub_tasks:
            task.cancel()
        if self._rep_socket is not None:
            self._rep_socket.close(linger=0)
        if self._rep_task is not None:
            self._rep_task.cancel()

    async def wait_closed(self) -> None:
        while self._running:
            await asyncio.sleep(0.1)

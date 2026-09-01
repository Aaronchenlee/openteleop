"""WebRTC remote transport - NAT traversal, internet deployments.

Design follows LiveKit Portal + dimos Hosted Teleop:

* Six logical channels multiplexed over a single PeerConnection (BUNDLE).
* ``cmd_unreliable`` uses ``ordered=False, maxRetransmits=0`` - an expired
  command is worse than no command, so never retransmit old teleoperation.
* ``state_reliable`` uses ordered reliable SCTP for JSON commands + ack.
* Video / audio ride on MediaStreamTracks (hardware-accelerated encode).
* Signaling (SDP + ICE exchange) happens once over HTTPS/WebSocket only.

To keep this file dependency-light and testable, signaling is pluggable:
pass a ``signaler`` object with ``exchange(offer, role) -> answer``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

try:
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters
except ImportError:  # pragma: no cover - import-guarded for environments without WebRTC
    RTCPeerConnection = None  # type: ignore
    RTCSessionDescription = None  # type: ignore

from ..config.settings import TeleopConfig
from .base import BaseTransport

DEFAULT_CMD_HZ = 50


class _LoopbackVideoTrack(MediaStreamTrack):
    """Minimal video track implementation.

    In production, subclass this and push hardware-encoded frames from the
    camera pipeline (NVENC / VPU). The stub is used for testing and demos.
    """

    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._frame_queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=4)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def recv(self):
        if not self._running:
            self._running = True
            self._task = asyncio.get_running_loop().create_task(self._pump())
        frame = await self._frame_queue.get()
        return frame

    async def _pump(self):
        # Production: read from camera encoder output.
        while True:
            await asyncio.sleep(0.033)
            frame = await self._next_encoded_frame()
            if frame is not None:
                try:
                    self._frame_queue.put_nowait(frame)
                except asyncio.QueueFull:
                    pass  # drop-oldest for video

    async def _next_encoded_frame(self):
        raise NotImplementedError("Subclass and wire to your encoder")


class RemoteWebRTCTransport(BaseTransport):
    """WebRTC transport.

    Channels (DataChannel / track names, matching dimos):

    * ``cmd_unreliable``        DataChannel, unreliable/unordered
    * ``state_reliable``        DataChannel, reliable/ordered
    * ``state_reliable_back``   DataChannel, reliable/ordered
    * ``tactile_unreliable``    DataChannel, unreliable/unordered
    * ``video``                 MediaStreamTrack
    * ``audio``                 MediaStreamTrack (optional)
    """

    def __init__(self, config: TeleopConfig, role: str = "operator", signaler: Optional[Any] = None):
        super().__init__(config)
        self.role = role  # "operator" or "robot"
        self._signaler = signaler
        self._pc: Optional[Any] = None
        self._channels: Dict[str, Any] = {}
        self._local_tracks: Dict[str, Any] = {}
        self._pending_acks: Dict[str, asyncio.Future] = {}
        self._cmd_cb: Optional[Callable] = None
        self._state_cb: Optional[Callable] = None
        self._telemetry_cb: Optional[Callable] = None
        self._video_cb: Optional[Callable] = None
        self._tactile_cb: Optional[Callable] = None
        self._closed = asyncio.Event()
        self._seq = 0

    # ---- helpers ----
    def _inc_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def _check_open(self, name: str) -> bool:
        ch = self._channels.get(name)
        return ch is not None and ch.readyState == "open"

    # ---- cmd_unreliable ----
    def send_command(self, action: bytes, timestamp_us: int, seq: int) -> None:
        if self._check_open("cmd_unreliable"):
            payload = timestamp_us.to_bytes(8, "little") + bytes([seq]) + action
            self._channels["cmd_unreliable"].send(payload)

    def on_command(self, callback: Callable) -> None:
        self._cmd_cb = callback

    # ---- state_reliable ----
    async def send_state(self, message: dict, timeout_s: float = 2.0) -> dict:
        if "nonce" not in message:
            message["nonce"] = str(uuid.uuid4())
        if not self._check_open("state_reliable"):
            return {"ok": False, "error": "channel_closed", "nonce": message["nonce"]}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_acks[message["nonce"]] = fut
        self._channels["state_reliable"].send(json.dumps(message))
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending_acks.pop(message["nonce"], None)
            return {"ok": False, "error": "timeout", "nonce": message["nonce"]}

    def on_state(self, callback: Callable) -> None:
        self._state_cb = callback

    # ---- state_reliable_back ----
    def send_telemetry(self, telemetry: dict) -> None:
        if self._check_open("state_reliable_back"):
            self._channels["state_reliable_back"].send(json.dumps(telemetry))

    def on_telemetry(self, callback: Callable) -> None:
        self._telemetry_cb = callback

    # ---- video ----
    def send_video_frame(self, frame: Any, timestamp_us: int) -> None:
        track = self._local_tracks.get("video")
        if track is not None and hasattr(track, "_frame_queue"):
            try:
                track._frame_queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def on_video_frame(self, callback: Callable) -> None:
        self._video_cb = callback

    # ---- tactile ----
    def send_tactile(self, data: bytes, timestamp_us: int) -> None:
        if self._check_open("tactile_unreliable"):
            payload = timestamp_us.to_bytes(8, "little") + data
            self._channels["tactile_unreliable"].send(payload)

    def on_tactile(self, callback: Callable) -> None:
        self._tactile_cb = callback

    # ---- DataChannel handlers ----
    def _make_channels(self) -> None:
        if RTCPeerConnection is None:
            raise RuntimeError("aiortc not available; install with: pip install openteleop[remote]")
        self._pc = RTCPeerConnection(
            configuration={
                "iceServers": [
                    {"urls": s} for s in self.config.stun_servers
                ] + self.config.turn_servers
            }
        )
        if self.role == "operator":
            # Operator is the offerer: create channels locally.
            self._channels["cmd_unreliable"] = self._pc.createDataChannel(
                "cmd_unreliable", ordered=False, maxRetransmits=0
            )
            self._channels["state_reliable"] = self._pc.createDataChannel(
                "state_reliable", ordered=True
            )
            self._channels["tactile_unreliable"] = self._pc.createDataChannel(
                "tactile_unreliable", ordered=False, maxRetransmits=0
            )
        else:
            # Robot is the answerer: channels arrive via on_datachannel.
            self._pc.on("datachannel", self._on_datachannel)
            self._pc.on("track", self._on_track)

    def _on_datachannel(self, channel) -> None:
        name = channel.label
        self._channels[name] = channel
        if name == "cmd_unreliable":
            channel.on("message", self._on_cmd_msg)
        elif name == "state_reliable":
            channel.on("message", self._on_state_msg)
        elif name == "tactile_unreliable":
            channel.on("message", self._on_tactile_msg)
        # state_reliable_back and others are created by the operator side
        # only if it is the offerer; the robot creates its own publisher.

    def _on_track(self, track) -> None:
        if track.kind == "video":
            self._video_track = track

    def _on_cmd_msg(self, message) -> None:
        raw = bytes(message)
        ts = int.from_bytes(raw[:8], "little")
        seq = raw[8]
        action = raw[9:]
        if self._cmd_cb:
            self._cmd_cb(action, ts, seq)
        self._stats["cmd_unreliable"].observe(ts, int(time.time() * 1e6))

    def _on_state_msg(self, message) -> None:
        data = json.loads(bytes(message).decode())
        ack = self._state_cb(data) if self._state_cb else {"ok": True}
        ack.setdefault("nonce", data.get("nonce"))
        ack.setdefault("type", "cmd_ack")
        if self._check_open("state_reliable_back"):
            self._channels["state_reliable_back"].send(json.dumps(ack))
        # Also resolve any local pending ack (loopback / robot-as-offerer).
        nonce = data.get("nonce")
        if nonce and nonce in self._pending_acks:
            self._pending_acks.pop(nonce).set_result(ack)

    def _on_tactile_msg(self, message) -> None:
        raw = bytes(message)
        ts = int.from_bytes(raw[:8], "little")
        if self._tactile_cb:
            self._tactile_cb(raw[8:], ts)
        self._stats["tactile_unreliable"].observe(ts, int(time.time() * 1e6))

    # ---- signaling + lifecycle ----
    async def connect(self) -> None:
        self._make_channels()
        if self._signaler is None:
            raise RuntimeError("A signaler is required for remote transport")
        if self.role == "operator":
            await self._pc.setLocalDescription(await self._pc.createOffer())
            answer = await self._signaler.exchange(self._pc.localDescription, "operator")
            await self._pc.setRemoteDescription(RTCSessionDescription(sdp=answer.sdp, type=answer.type))
        else:
            offer = await self._signaler.exchange(None, "robot")
            await self._pc.setRemoteDescription(offer)
            await self._pc.setLocalDescription(await self._pc.createAnswer())
            await self._signaler.exchange(self._pc.localDescription, "robot")
        await self._wait_connected()

    async def _wait_connected(self, timeout_s: float = 15.0) -> None:
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def _on_conn_state() -> None:
            if self._pc.connectionState in ("connected", "completed"):
                if not done.done():
                    done.set_result(True)
            elif self._pc.connectionState in ("failed", "closed", "disconnected"):
                if not done.done():
                    done.set_exception(RuntimeError(f"ICE {self._pc.connectionState}"))

        self._pc.on("connectionstatechange", _on_conn_state)
        try:
            await asyncio.wait_for(done, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("WebRTC connection timed out") from exc

    async def close(self) -> None:
        self._running = False
        if self._pc is not None:
            await self._pc.close()
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

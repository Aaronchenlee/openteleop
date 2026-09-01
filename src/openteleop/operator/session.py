"""Operator-side session - orchestrates input -> retarget -> command -> display."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..channels.command import CommandChannel, StateChannel
from ..config.settings import TeleopConfig
from ..retarget.retargeter import Retargeter, build_retargeter
from ..safety.safety import VideoGuard
from ..sync.sync_buffer import SyncBuffer
from ..transport.base import BaseTransport
from ..transport.factory import TransportFactory
from .input_adapter import InputAdapter, SineWaveAdapter


class OperatorSession:
    """End-to-end operator loop.

    Pipeline per control tick::

        InputAdapter -> Retargeter -> CommandChannel (rate+vel limit) -> Transport

    Feedback loop::

        Transport <- SyncBuffer (align video/state/tactile) -> display callbacks

    Safety: L4 video-freeze gate blocks command input on stale video.
    """

    def __init__(
        self,
        config: TeleopConfig,
        transport: Optional[BaseTransport] = None,
        input_adapter: Optional[InputAdapter] = None,
        retargeter: Optional[Retargeter] = None,
    ):
        self.config = config
        self._transport = transport or TransportFactory.create(
            config, role="operator", signaler=None
        )
        # If a transport was injected, ensure its role matches.
        if transport is not None and getattr(transport, "role", "operator") != "operator":
            transport.role = "operator"
        self._input = input_adapter or SineWaveAdapter(n_dof=config.n_dof)
        self._retarget = retargeter or build_retargeter(config.retarget, config.n_dof)
        # Transport-dependent components are created in start() so that an
        # injected transport (set after construction) is always the one used.
        self._commands = None
        self._state = None
        self._sync = SyncBuffer(
            video_fps=config.video.fps, on_observation=self._on_observation
        )
        self._video_guard = VideoGuard(
            config.safety,
            on_freeze=self._on_freeze,
            enabled=config.safety.enable_video_guard,
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.observations: list = []
        self.telemetry: dict = {}
        self.latest_frame = None


    @property
    def transport(self) -> BaseTransport:
        return self._transport

    def _on_video_frame(self, frame, timestamp_us: int) -> None:
        self.latest_frame = frame
        self._video_guard.on_frame()
        self._sync.add_frame("main", frame, timestamp_us)

    def _on_tactile(self, data: bytes, timestamp_us: int) -> None:
        self._sync.add_tactile(data, timestamp_us)

    def _on_telemetry(self, telemetry: dict) -> None:
        self.telemetry = telemetry

    def _on_observation(self, obs) -> None:
        self.observations.append(obs)
        if len(self.observations) > 10:
            self.observations.pop(0)

    def _on_freeze(self, frozen: bool) -> None:
        # Override in subclass to flash the UI warning.
        pass

    def _init_transport_dependents(self) -> None:
        """Create components bound to the final transport."""
        self._commands = CommandChannel(self._transport, n_dof=self.config.n_dof)
        self._commands.ctrl_hz = self.config.ctrl_hz
        self._state = StateChannel(self._transport)

    def _bind_callbacks(self) -> None:
        """Wire inbound callbacks after the final transport is set."""
        self._transport.on_video_frame(self._on_video_frame)
        self._transport.on_telemetry(self._on_telemetry)
        self._transport.on_tactile(self._on_tactile)
    async def start(self) -> None:
        self._init_transport_dependents()
        self._bind_callbacks()
        await self._transport.connect()
        self._running = True
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.config.ctrl_hz
        while self._running:
            t0 = loop.time()
            if self._input.has_new_sample() and not self._video_guard.frozen:
                human = self._input.read_action()
                target = self._retarget.retarget(human)
                self._commands.send(target, dt=interval)
            await asyncio.sleep(max(0.0, interval - (loop.time() - t0)))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        await self._transport.close()

    async def send_command(self, cmd_type: str, payload: dict) -> dict:
        """Send a reliable command (mode switch, config, e-stop, ...)."""
        return await self._state.send(cmd_type, payload)

    def get_stats(self) -> dict:
        return self._transport.get_stats()

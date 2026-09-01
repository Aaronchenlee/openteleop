"""Robot-side session - orchestrates receive -> route -> execute -> feedback."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..channels.command import TelemetryChannel
from ..config.settings import TeleopConfig
from ..robot.controller import CommandRouter, DummyRobot, RobotController, SharedAutonomyLayer
from ..safety.safety import DualPathEstop, HeartbeatMonitor, SafetyMonitor
from ..transport.base import BaseTransport
from ..transport.factory import TransportFactory


class RobotSession:
    """End-to-end robot loop.

    Pipeline per control tick::

        Transport (cmd_unreliable) -> CommandRouter (vel-limit + residual) -> RobotController

    Feedback loop::

        RobotController -> telemetry publisher -> Transport (state_reliable_back)
        RobotController -> camera/tactile -> Transport (video/tactile)

    Safety: L1 heartbeat deadman + L2 dual-path e-stop + L3 local monitor.
    """

    def __init__(
        self,
        config: TeleopConfig,
        transport: Optional[BaseTransport] = None,
        robot: Optional[RobotController] = None,
    ):
        self.config = config
        self._transport = transport or TransportFactory.create(
            config, role="robot", signaler=None
        )
        # If a transport was injected, ensure its role matches.
        if transport is not None and getattr(transport, "role", "robot") != "robot":
            transport.role = "robot"
        self.robot = robot or DummyRobot(n_dof=config.n_dof)
        self._autonomy = SharedAutonomyLayer(
            self.robot, force_limit=config.safety.ee_force_limit
        )
        self._router = CommandRouter(self.robot, config.safety, shared_autonomy=self._autonomy)
        self._router.install_defaults()
        # Transport-dependent components are created in start() so that an
        # injected transport (set after construction) is always the one used.
        self._telemetry: Optional[TelemetryChannel] = None
        self._estop: Optional[DualPathEstop] = None
        # Safety stack (transport-independent).
        self._heartbeat = HeartbeatMonitor(
            config.safety, on_deadman=self._on_deadman
        )
        self._safety_monitor = SafetyMonitor(config.safety, read_hw=self.robot.read_state)
        self._safety_monitor.set_estop_callback(self._on_hw_estop)
        self._running = False
        self._telemetry_task: Optional[asyncio.Task] = None

    @property
    def transport(self) -> BaseTransport:
        return self._transport

    def _handle_inbound_state(self, message: dict) -> dict:
        """Route state commands through the router and safety stack."""
        if message.get("type") == "ping":
            self._heartbeat.on_heartbeat()
            return {"ok": True, "type": "pong"}
        if message.get("type") == "e_stop":
            return self._estop.handle_estop(message)
        return self._router.handle_state(message)

    # ---- safety callbacks ----
    def _on_deadman(self) -> None:
        self.robot.ramp_down_to_zero(self.config.safety.ramp_down_ms / 1000.0)

    def _on_estop(self, level: str) -> None:
        if level == "full":
            self.robot.emergency_stop()

    def _on_hw_estop(self, violation: str) -> None:
        self.robot.emergency_stop()

    def _init_transport_dependents(self) -> None:
        """Create components bound to the final transport."""
        self._telemetry = TelemetryChannel(self._transport, self.config.telemetry_hz)
        self._estop = DualPathEstop(self._transport, self.config.safety, on_estop=self._on_estop)

    def _bind_callbacks(self) -> None:
        """Wire transport callbacks (must be called after the final
        transport is set, i.e. inside start())."""
        self._transport.on_command(self._router.on_command)
        self._transport.on_state(self._handle_inbound_state)

    async def start(self) -> None:
        self._init_transport_dependents()
        self._bind_callbacks()
        await self._transport.connect()
        self._running = True
        self._heartbeat.start()
        self._safety_monitor.start()
        self._telemetry_task = asyncio.get_running_loop().create_task(self._telemetry_loop())

    async def _telemetry_loop(self) -> None:
        while self._running:
            state = self.robot.read_state()
            telemetry = {
                "robot_state": {
                    k: (v.tolist() if hasattr(v, "tolist") else v)
                    for k, v in state.items()
                },
                "mode": self._router.mode,
                "estopped": self._estop.estopped,
                "heartbeat_alive": self._heartbeat.alive,
            }
            self._telemetry.publish(telemetry)
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self._running = False
        self._heartbeat.stop()
        self._safety_monitor.stop()
        if self._telemetry_task:
            self._telemetry_task.cancel()
        await self._transport.close()

    def send_telemetry(self, data: dict) -> None:
        """Robot -> operator telemetry for e.g. e-stop state broadcast."""
        self._telemetry.publish(data)

    def get_stats(self) -> dict:
        return self._transport.get_stats()

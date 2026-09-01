"""Command-line interface.

Usage::

    openteleop operator [--local] [--peer IP] [--hz 50]
    openteleop robot    [--local] [--n-dof 6]
    openteleop demo     [--seconds 5]   # operator+robot on one process
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..config.settings import TeleopConfig, TransportMode
from ..operator.session import OperatorSession
from ..robot.session import RobotSession


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openteleop",
        description="Production-grade open-source teleoperation framework",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    op = sub.add_parser("operator", help="Run the operator station")
    op.add_argument("--local", action="store_true", help="Force local ZMQ transport")
    op.add_argument("--peer", default="127.0.0.1", help="Robot IP for local mode")
    op.add_argument("--hz", type=float, default=50.0, help="Control rate (Hz)")
    op.add_argument("--operator-id", default="operator-001")

    rb = sub.add_parser("robot", help="Run the robot station")
    rb.add_argument("--local", action="store_true", help="Force local ZMQ transport")
    rb.add_argument("--n-dof", type=int, default=6, help="Robot DOF")
    rb.add_argument("--robot-id", default="robot-001")

    dm = sub.add_parser("demo", help="Run operator+robot on one process")
    dm.add_argument("--seconds", type=float, default=5.0, help="Demo duration")
    dm.add_argument("--n-dof", type=int, default=6)
    return parser


def _local_config(kind: str, **kwargs) -> TeleopConfig:
    cfg = TeleopConfig.default_robot() if kind == "robot" else TeleopConfig.default_operator()
    cfg.mode = TransportMode.LOCAL
    for k, v in kwargs.items():
        if v is not None:
            setattr(cfg, k, v)
    return cfg


async def _run_operator(args) -> int:
    cfg = _local_config("operator", operator_id=args.operator_id)
    cfg.ctrl_hz = args.hz
    session = OperatorSession(cfg)
    # Explicitly create with role + peer so ZMQ binds/connects correctly.
    from ..transport.local_zmq import LocalZMQTransport

    session._transport = LocalZMQTransport(cfg, role="operator", peer_ip=args.peer)
    await session.start()
    print(f"[operator] started, ctrl {args.hz}Hz, transport=zmq peer={args.peer}")
    try:
        await asyncio.Event().wait()
    finally:
        await session.stop()
    return 0


async def _run_robot(args) -> int:
    cfg = _local_config("robot", robot_id=args.robot_id)
    cfg.n_dof = args.n_dof
    session = RobotSession(cfg)
    await session.start()
    print(f"[robot] started, {args.n_dof}DoF, transport=zmq")
    try:
        await asyncio.Event().wait()
    finally:
        await session.stop()
    return 0


async def _run_demo(args) -> int:
    """Operator + robot on one process over loopback ZMQ (no network needed)."""
    import numpy as np

    from ..operator.input_adapter import SineWaveAdapter

    cfg = TeleopConfig.default_robot()
    cfg.mode = TransportMode.LOCAL
    cfg.n_dof = args.n_dof
    cfg.ctrl_hz = 50.0
    cfg.safety.enable_video_guard = False

    robot_session = RobotSession(cfg)
    op_session = OperatorSession(
        cfg,
        input_adapter=SineWaveAdapter(n_dof=args.n_dof, freq_hz=0.8),
    )
    # Force loopback transport so both sides share one host.
    from ..transport.local_zmq import LocalZMQTransport

    robot_session._transport = LocalZMQTransport(cfg, role="robot", peer_ip="127.0.0.1")
    op_session._transport = LocalZMQTransport(cfg, role="operator", peer_ip="127.0.0.1")

    await robot_session.start()
    await op_session.start()
    print(f"[demo] running {args.seconds}s ...")

    # Enable teleop mode so commands flow through the router.
    await op_session.send_command("set_mode", {"mode": "teleop"})
    await asyncio.sleep(args.seconds)
    await op_session.send_command("set_mode", {"mode": "idle"})

    stats = op_session.get_stats()
    print(f"[demo] command channel: {stats.get('cmd_unreliable')}")
    print(f"[demo] robot pos: {robot_session.robot.read_state()['joint_positions']}")
    await robot_session.stop()
    await op_session.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "operator":
        return asyncio.run(_run_operator(args))
    if args.command == "robot":
        return asyncio.run(_run_robot(args))
    if args.command == "demo":
        return asyncio.run(_run_demo(args))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

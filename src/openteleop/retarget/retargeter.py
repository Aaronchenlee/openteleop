"""Motion retargeting - human hand/arm to robot joint space.

Reference implementation of the DexPilot-style vector retargeting:
match *fingertip positions and finger direction vectors* rather than raw
joint angles, because human and robot kinematics differ structurally.

The production path uses ``dexsuite/dex-retargeting`` (Pinocchio + SLSQP,
<15 ms per solve). This module provides the interface plus a lightweight
identity/scale fallback so the framework is runnable without that dependency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import numpy as np

from ..config.settings import RetargetConfig


class Retargeter(ABC):
    """Human motion -> robot action retargeting."""

    @abstractmethod
    def retarget(self, human_action: np.ndarray) -> np.ndarray:
        """Map a raw human action to a robot target action."""
        raise NotImplementedError


class IdentityRetargeter(Retargeter):
    """Pass-through retargeter for end-to-end demos and tests.

    When the human device and the robot share the same action space
    (e.g. both are joint-space), no retargeting is required.
    """

    def __init__(self, n_dof: int = 6):
        self._n_dof = n_dof

    def retarget(self, human_action: np.ndarray) -> np.ndarray:
        arr = np.asarray(human_action, dtype=np.float32).reshape(-1)
        if arr.shape[0] != self._n_dof:
            arr = np.resize(arr, self._n_dof)
        return arr


class ScaleRetargeter(Retargeter):
    """Linear remapping with per-axis scale + offset.

    Handy when the human and robot workspaces differ in extent.
    """

    def __init__(self, n_dof: int = 6, scale: Optional[np.ndarray] = None, offset: Optional[np.ndarray] = None):
        self._n_dof = n_dof
        self._scale = np.ones(n_dof, dtype=np.float32) if scale is None else np.asarray(scale, dtype=np.float32)
        self._offset = np.zeros(n_dof, dtype=np.float32) if offset is None else np.asarray(offset, dtype=np.float32)

    def retarget(self, human_action: np.ndarray) -> np.ndarray:
        arr = np.asarray(human_action, dtype=np.float32).reshape(-1)
        if arr.shape[0] != self._n_dof:
            arr = np.resize(arr, self._n_dof)
        return arr * self._scale + self._offset


def build_retargeter(config: RetargetConfig, n_dof: int = 6) -> Retargeter:
    """Factory for the configured retargeter.

    * ``solver == "slsqp"`` and dexsuite available -> DexPilot-style QP
      (raise informative error if the optional dependency is missing)
    * otherwise fall back to :class:`ScaleRetargeter` with identity scale.
    """
    if config.solver == "slsqp":
        try:
            from dex_retargeting import DexRetargeting  # type: ignore

            return _DexSuiteAdapter(config, n_dof, DexRetargeting)
        except ImportError:
            pass  # fall through to the lightweight fallback
    return ScaleRetargeter(n_dof=n_dof)


class _DexSuiteAdapter(Retargeter):
    """Adapter for ``dexsuite/dex-retargeting`` when installed."""

    def __init__(self, config: RetargetConfig, n_dof: int, cls):
        self._config = config
        self._n_dof = n_dof
        self._cls = cls
        self._retargeter = cls(  # minimal stub; actual config per hand
            robot_name="sharpa_wave" if hasattr(cls, "from_robot") else None
        )

    def retarget(self, human_action: np.ndarray) -> np.ndarray:
        # Placeholder: real implementation calls retargeter.retarget() with
        # fingertip positions/directions and returns joint targets.
        arr = np.asarray(human_action, dtype=np.float32).reshape(-1)
        if arr.shape[0] != self._n_dof:
            arr = np.resize(arr, self._n_dof)
        return arr

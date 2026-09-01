"""Operator-side input adapters.

:class:`InputAdapter` is the abstraction for any human input device
(VR headset + controllers, data glove, keyboard/joystick for demos).
A reference keyboard demo adapter is provided so the pipeline can run
without VR hardware.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


class InputAdapter(ABC):
    """Abstract human input source."""

    @abstractmethod
    def read_action(self) -> np.ndarray:
        """Return the current raw human action (e.g. EE pose or joint target)."""
        raise NotImplementedError

    @abstractmethod
    def has_new_sample(self) -> bool:
        raise NotImplementedError


class KeyboardAdapter(InputAdapter):
    """Reference keyboard input for demos and tests.

    Arrow keys move the first two DOFs, PageUp/PageDown and +/- adjust the
    rest. Not for production use - a VR / glove adapter should implement
    :class:`InputAdapter` instead.
    """

    def __init__(self, n_dof: int = 6):
        self._n_dof = n_dof
        self._action = np.zeros(n_dof, dtype=np.float32)
        self._new = False
        self._lock = threading.Lock()
        self._keys: dict[str, bool] = {}

    def press(self, key: str) -> None:
        with self._lock:
            self._keys[key] = True
            self._apply()

    def release(self, key: str) -> None:
        with self._lock:
            self._keys[key] = False

    def _apply(self) -> None:
        step = 0.02
        if self._keys.get("up"):
            self._action[0] += step
        if self._keys.get("down"):
            self._action[0] -= step
        if self._keys.get("left"):
            self._action[1] -= step
        if self._keys.get("right"):
            self._action[1] += step
        if self._keys.get("pageup"):
            self._action[2] += step
        if self._keys.get("pagedown"):
            self._action[2] -= step
        if self._keys.get("+"):
            self._action[3] += step
        if self._keys.get("-"):
            self._action[3] -= step
        self._new = True

    def has_new_sample(self) -> bool:
        with self._lock:
            return self._new

    def read_action(self) -> np.ndarray:
        with self._lock:
            self._new = False
            return self._action.copy()


class SineWaveAdapter(InputAdapter):
    """Programmatic input for testing - drives a smooth trajectory."""

    def __init__(self, n_dof: int = 6, freq_hz: float = 1.0, amplitude: float = 0.3):
        self._n_dof = n_dof
        self._freq = freq_hz
        self._amp = amplitude
        self._t0 = time.monotonic()

    def has_new_sample(self) -> bool:
        return True

    def read_action(self) -> np.ndarray:
        t = time.monotonic() - self._t0
        phase = 2 * np.pi * self._freq * t
        return self._amp * np.sin(phase + np.arange(self._n_dof))

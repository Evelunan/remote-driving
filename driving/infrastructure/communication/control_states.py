from typing import NamedTuple
import time


class ControlSnapshot(NamedTuple):
    linear: float
    angular: float
    timestamp: float


class ControlState:

    def __init__(self):
        self._snapshot = ControlSnapshot(0.0, 0.0, time.monotonic())

    def update(self, linear: float, angular: float):
        self._snapshot = ControlSnapshot(linear, angular, time.monotonic())

    def get_snapshot(self) -> ControlSnapshot:
        return self._snapshot

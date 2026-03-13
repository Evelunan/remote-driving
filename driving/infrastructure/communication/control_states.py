from typing import NamedTuple
import time


class ControlSnapshot(NamedTuple):
    linear: int
    angular: int
    timestamp: float


class ControlState:

    def __init__(self):
        self._snapshot = ControlSnapshot(0.0, 0.0, time.monotonic())

    def update(self, linear: int, angular: int):
        self._snapshot = ControlSnapshot(linear, angular, time.monotonic())

    def get_snapshot(self) -> ControlSnapshot:
        return self._snapshot

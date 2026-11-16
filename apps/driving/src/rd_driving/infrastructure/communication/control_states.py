from typing import NamedTuple
import time


class ControlSnapshot(NamedTuple):
    linear: int
    angular: int
    timestamp: float


class ControlState:

    def __init__(self, linear=0, angular=0):
        self._snapshot = ControlSnapshot(linear, angular, time.monotonic())

    def update(self, linear: int, angular: int):
        self._snapshot = ControlSnapshot(linear, angular, time.monotonic())

    def get_snapshot(self) -> ControlSnapshot:
        return self._snapshot

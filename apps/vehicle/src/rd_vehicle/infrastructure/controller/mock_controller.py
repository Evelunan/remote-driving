"""Deterministic in-memory actuator for simulation; never claims physical stop."""

import threading
from collections import deque

from rd_core.settings import MotionSettings
from rd_vehicle.interfaces.icontroller import IController, StopResult

from .motion import target_velocity


class MockController(IController):
    def __init__(self, motion: MotionSettings):
        self.motion = motion
        self.commands = deque(maxlen=1024)
        self.stop_requests = deque(maxlen=64)
        self.closed = False
        self._lock = threading.Lock()

    def send_command(self, throttle, brake, steering):
        velocity = target_velocity(
            throttle,
            brake,
            steering,
            self.motion.max_linear_mps,
            self.motion.max_angular_radps,
        )
        with self._lock:
            if self.closed:
                raise RuntimeError("mock actuator is closed")
            self.commands.append(velocity)

    def request_safe_stop(self, reason):
        self.send_command(0, 1, 0)
        with self._lock:
            self.stop_requests.append(reason)
        return StopResult(
            requested=True,
            confirmed=False,
            detail="simulated zero target; no physical feedback",
        )

    def close(self):
        if not self.closed:
            self.request_safe_stop("mock controller closed")
            self.closed = True

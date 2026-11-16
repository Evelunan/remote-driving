"""Capture timestamps survive processing, buffering, and repeated transmission."""

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RawInput:
    data: bytes | None
    sampled_at: float
    timestamp_ms: int

    @classmethod
    def capture(cls, data: bytes | None):
        return cls(data, time.monotonic(), time.time_ns() // 1_000_000)


@dataclass(frozen=True)
class ControlInput:
    throttle: float
    brake: float
    steering: float
    sampled_at: float
    timestamp_ms: int
    valid: bool = True

    def fresh(self, timeout_s: float, *, now: float | None = None) -> bool:
        age = (time.monotonic() if now is None else now) - self.sampled_at
        values = (self.throttle, self.brake, self.steering, age)
        return (
            self.valid
            and all(math.isfinite(value) for value in values)
            and 0 <= age < timeout_s
            and -1 <= self.throttle <= 1
            and 0 <= self.brake <= 1
            and -1 <= self.steering <= 1
        )

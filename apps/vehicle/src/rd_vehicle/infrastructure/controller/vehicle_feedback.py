"""Chassis mapping and latest feedback, independent of the ROS runtime."""

import math
import threading
import time
from dataclasses import dataclass

from rd_core.settings import VehicleFeedbackSettings
from rd_core.wire import VehicleStatus, conservative_age_ms


@dataclass(frozen=True)
class FeedbackSample:
    status: VehicleStatus
    source_time_ms: int
    received_at: float
    revision: int


def _number(value, scale=1.0):
    if type(value) not in (int, float):
        return None
    try:
        scaled = value * scale
    except OverflowError:
        return None
    return float(scaled) if math.isfinite(scaled) and abs(scaled) <= 3.4028234e38 else None


def _code(value):
    return value if type(value) is int and 0 <= value <= 255 else None


def _flag(value):
    return value if type(value) is bool else None


class VehicleStatusBuffer:
    def __init__(self, settings: VehicleFeedbackSettings):
        self.settings = settings
        self._lock = threading.Lock()
        self._latest = None
        self._revision = 0

    def receive(self, message):
        stamp = getattr(getattr(message, "header", None), "stamp", None)
        sec, nanosec = getattr(stamp, "sec", None), getattr(stamp, "nanosec", None)
        if (type(sec) is not int or sec < 0 or type(nanosec) is not int
                or not 0 <= nanosec < 1_000_000_000):
            return False
        source_time_ms = sec * 1000 + nanosec // 1_000_000
        try:
            conservative_age_ms(source_time_ms, time.time_ns() // 1_000_000,
                                max_age_ms=self.settings.stale_timeout_ms, clock_skew_ms=0)
        except ValueError:
            return False
        if source_time_ms == 0:
            return False
        scale = 1.0 if self.settings.wheel_speed_unit == "m/s" else 1.0 / 3.6
        status = VehicleStatus(
            speed_mps=_number(getattr(message, "cur_speed", None)),
            steering_deg=_number(getattr(message, "cur_steer", None)),
            left_wheel_mps=_number(getattr(message, "left_wheel_speed", None), scale),
            right_wheel_mps=_number(getattr(message, "right_wheel_speed", None), scale),
            shift_level=_code(getattr(message, "shift_level", None)),
            is_autodrive=_flag(getattr(message, "is_autodrive", None)),
            drive_mode=_code(getattr(message, "drive_mode", None)),
            p_status=_flag(getattr(message, "p_status", None)),
        )
        with self._lock:
            if self._latest is not None and source_time_ms <= self._latest.source_time_ms:
                return False
            self._revision += 1
            self._latest = FeedbackSample(status, source_time_ms, time.monotonic(), self._revision)
        return True

    def latest(self):
        with self._lock:
            sample = self._latest
        if sample is None or time.monotonic() - sample.received_at >= self.settings.stale_timeout_ms / 1000:
            return None
        try:
            conservative_age_ms(sample.source_time_ms, time.time_ns() // 1_000_000,
                                max_age_ms=self.settings.stale_timeout_ms, clock_skew_ms=0)
        except ValueError:
            return None
        return sample

    def clear(self):
        with self._lock:
            self._latest = None

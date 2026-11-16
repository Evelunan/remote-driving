"""Normalized input to SI target velocity; this is not measured odometry."""

import math


def target_velocity(throttle, brake, steering, max_linear, max_angular):
    values = (throttle, brake, steering, max_linear, max_angular)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("motion values must be finite")
    if not (-1 <= throttle <= 1 and 0 <= brake <= 1 and -1 <= steering <= 1):
        raise ValueError("normalized control is outside its range")
    if max_linear <= 0 or max_angular <= 0:
        raise ValueError("motion limits must be positive")
    # Without odometry or a chassis braking contract, request zero velocity.
    # Never invent measured velocity from the previous command.
    if brake > 0:
        return 0.0, 0.0
    return throttle * max_linear, steering * max_angular

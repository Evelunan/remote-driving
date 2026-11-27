"""Control expiry and bounded device execution, independent of the network loop.

The independent stop hook is an integration point for a chassis safety channel.
Neither a software timeout nor a zero target proves that a vehicle has stopped.
"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Controller(Protocol):
    def send_command(self, throttle: float, brake: float, steering: float): ...
    def request_safe_stop(self, reason: str): ...
    def close(self): ...


class ControlState(str, Enum):
    __str__ = str.__str__

    DISARMED = "disarmed"
    READY = "ready"
    WAITING_NEUTRAL = "waiting_neutral"
    ACTIVE = "active"
    FAILSAFE = "failsafe"
    CLOSED = "closed"


@dataclass(frozen=True)
class Command:
    epoch: str
    seq: int
    throttle: float
    brake: float
    steering: float
    source_timestamp: int = 0

    @property
    def neutral(self) -> bool:
        return self.throttle == 0 and self.steering == 0

    def validate(self):
        if (
            not self.epoch
            or isinstance(self.seq, bool)
            or not isinstance(self.seq, int)
        ):
            raise ValueError("invalid command identity")
        if not all(
            math.isfinite(v) for v in (self.throttle, self.brake, self.steering)
        ):
            raise ValueError("control values must be finite")
        if not (
            -1 <= self.throttle <= 1
            and 0 <= self.brake <= 1
            and -1 <= self.steering <= 1
        ):
            raise ValueError("control outside normalized range")


def is_newer(seq: int, previous: int | None, bits: int = 32) -> bool:
    modulus = 1 << bits
    if not 0 <= seq < modulus:
        return False
    return previous is None or 0 < ((seq - previous) % modulus) < modulus // 2


class ControlSupervisor:
    """Single latest target, serialized device I/O, and a separate timeout monitor.

    A failsafe remains latched until a new epoch is bound. Legacy sessions also
    require a neutral command. Heartbeats never call submit and never renew input.
    A blocked device operation cannot be killed safely; the independent stop hook
    is notified and pending motion is discarded. Chassis watchdog integration is
    still required for physical safety.
    """

    def __init__(
        self,
        controller: Controller | None,
        *,
        timeout_s: float,
        sequence_bits: int = 32,
        independent_stop: Callable[[str], None] | None = None,
        on_result: Callable[[Command, bool, str], None] | None = None,
    ):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("control timeout must be finite and positive")
        if sequence_bits not in (16, 32):
            raise ValueError("sequence width must be 16 or 32")
        self.controller = controller
        self.timeout_s = timeout_s
        self.sequence_bits = sequence_bits
        self.on_result = on_result
        self._independent_stop = independent_stop
        self._condition = threading.Condition()
        self._state = ControlState.DISARMED
        self._reason = "not connected"
        self._epoch: str | None = None
        self._last_seq: int | None = None
        self._deadline: float | None = None
        self._pending: Command | None = None
        self._pending_stop: str | None = None
        self._safety_notice: str | None = None
        self._safety_busy = False
        self._closing = False
        self._device_busy_since: float | None = None
        self._threads = [
            threading.Thread(target=self._execute, name="rd-device", daemon=True),
            threading.Thread(target=self._watchdog, name="rd-watchdog", daemon=True),
        ]
        if independent_stop is not None:
            self._threads.append(
                threading.Thread(
                    target=self._safety_loop,
                    name="rd-safety-hook",
                    daemon=True,
                )
            )
        for thread in self._threads:
            thread.start()

    @property
    def state(self) -> ControlState:
        with self._condition:
            return self._state

    @property
    def reason(self) -> str:
        with self._condition:
            return self._reason

    def begin_session(self, epoch: str):
        if not epoch:
            raise ValueError("session epoch cannot be empty")
        with self._condition:
            if self._closing:
                raise RuntimeError("supervisor is closed")
            if epoch == self._epoch:
                raise ValueError("recovery requires a new session epoch")
            if self._device_busy_since is not None:
                raise RuntimeError("previous device operation has not completed")
            self._trip_locked("new session requires neutral input")
            self._epoch = epoch
            self._last_seq = None
            self._deadline = None
            self._state = ControlState.WAITING_NEUTRAL

    def begin_route(self, epoch: str) -> bool:
        """Bind an idle authenticated route when the previous stop has finished."""
        with self._condition:
            if not epoch or epoch == self._epoch:
                raise ValueError("direct control requires a new route epoch")
            if (
                self._closing
                or self._state == ControlState.ACTIVE
                or self._device_busy_since is not None
                or self._pending_stop is not None
                or self._safety_notice is not None
                or self._safety_busy
            ):
                return False
            self._epoch = epoch
            self._last_seq = self._deadline = None
            self._state = ControlState.READY
            self._reason = "waiting for a fresh command"
            return True

    def submit(
        self, command: Command, *, age_s: float = 0.0, deadline: float | None = None
    ) -> bool:
        try:
            command.validate()
        except (ValueError, TypeError):
            return False
        if not math.isfinite(age_s) or age_s < 0 or age_s > self.timeout_s:
            return False
        with self._condition:
            if self._closing or command.epoch != self._epoch:
                return False
            if (
                self._state == ControlState.ACTIVE
                and self._deadline is not None
                and time.monotonic() >= self._deadline
            ):
                self._trip_locked("control input expired before next command")
                return False
            if self._state not in (
                ControlState.READY, ControlState.ACTIVE, ControlState.WAITING_NEUTRAL
            ):
                return False
            if not is_newer(command.seq, self._last_seq, self.sequence_bits):
                return False
            if self._state == ControlState.WAITING_NEUTRAL and not command.neutral:
                return False
            now = time.monotonic()
            expires = now + self.timeout_s - age_s
            if deadline is not None:
                if not math.isfinite(deadline):
                    return False
                expires = min(expires, deadline)
            if now >= expires:
                return False
            self._state = ControlState.ACTIVE
            self._last_seq = command.seq
            # Age consumed upstream reduces the remaining local watchdog budget.
            self._deadline = expires
            self._pending = command
            self._condition.notify_all()
            return True

    def stop(self, reason: str, *, epoch: str | None = None):
        with self._condition:
            if self._closing or (epoch is not None and epoch != self._epoch):
                return
            self._trip_locked(reason)

    def _trip_locked(self, reason: str):
        self._state = ControlState.CLOSED if self._closing else ControlState.FAILSAFE
        self._reason = reason
        self._pending = None
        self._deadline = None
        self._pending_stop = reason
        if self._independent_stop is not None:
            self._safety_notice = reason
        self._condition.notify_all()

    def _watchdog(self):
        with self._condition:
            while not self._closing:
                now = time.monotonic()
                if self._state == ControlState.ACTIVE:
                    if self._deadline is not None and now >= self._deadline:
                        self._trip_locked("control input expired")
                    elif (
                        self._device_busy_since is not None
                        and now - self._device_busy_since >= self.timeout_s
                    ):
                        self._trip_locked("device execution timed out")
                wait = min(self.timeout_s / 4, 0.05)
                if self._state == ControlState.ACTIVE and self._deadline is not None:
                    wait = min(wait, max(0, self._deadline - time.monotonic()))
                self._condition.wait(timeout=wait)

    def _execute(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: (
                            self._closing
                            or self._pending_stop is not None
                            or self._pending is not None
                        )
                    )
                    stop_reason, command = self._pending_stop, self._pending
                    self._pending_stop = None
                    if stop_reason is not None:
                        command = None  # Always execute stop before a newer target.
                    else:
                        self._pending = None
                    if stop_reason is None and command is None and self._closing:
                        break
                    if command is not None and (
                        self._state != ControlState.ACTIVE
                        or command.epoch != self._epoch
                    ):
                        continue
                    if (
                        command is not None
                        and self._deadline is not None
                        and time.monotonic() >= self._deadline
                    ):
                        self._trip_locked("control input expired before execution")
                        continue
                    self._device_busy_since = time.monotonic()
                error = None
                try:
                    if self.controller is not None:
                        if stop_reason is not None:
                            self.controller.request_safe_stop(stop_reason)
                        elif command is not None:
                            self.controller.send_command(
                                command.throttle, command.brake, command.steering
                            )
                except Exception as exc:  # noqa: BLE001 - actuator boundary must fail closed
                    error = type(exc).__name__
                with self._condition:
                    self._device_busy_since = None
                    if error is not None:
                        # A failed stop must not spin forever retrying on the same device.
                        self._trip_locked("device execution failed: " + error)
                        if stop_reason is not None:
                            self._pending_stop = None
                    current = (
                        not self._closing
                        and command is not None
                        and command.epoch == self._epoch
                    )
                    active = self._state == ControlState.ACTIVE
                    self._condition.notify_all()
                if command is not None and self.on_result is not None and current:
                    try:
                        executed = (
                            active and error is None and self.controller is not None
                        )
                        detail = error or (
                            "executed"
                            if executed
                            else "invalidated"
                            if not active
                            else "controller disabled"
                        )
                        self.on_result(command, executed, detail)
                    except Exception:  # noqa: BLE001 - callback failure must not kill the worker
                        self.stop("execution result delivery failed")
        finally:
            if self.controller is not None:
                try:
                    self.controller.close()
                except Exception:  # noqa: BLE001 - report adapter cleanup failure
                    with self._condition:
                        self._reason = "device close failed"

    def _safety_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closing or self._safety_notice is not None
                )
                notice = self._safety_notice
                self._safety_notice = None
                if notice is None and self._closing:
                    return
                self._safety_busy = notice is not None
            if notice is not None:
                try:
                    self._independent_stop(notice)
                except Exception:  # noqa: BLE001 - report external safety adapter failure
                    with self._condition:
                        self._reason = "independent safety hook failed"
                finally:
                    with self._condition:
                        self._safety_busy = False

    def close(self, timeout_s: float = 1.0) -> bool:
        with self._condition:
            if not self._closing:
                self._trip_locked("control supervisor shutdown")
                self._closing = True
                self._state = ControlState.CLOSED
                self._condition.notify_all()
        deadline = time.monotonic() + timeout_s
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

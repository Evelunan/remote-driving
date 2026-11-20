import threading
import time

import pytest
from rd_core.control import Command, ControlState, ControlSupervisor


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    assert predicate()


class Recorder:
    def __init__(self):
        self.commands = []
        self.stops = []
        self.closed = False

    def send_command(self, *values):
        self.commands.append(values)

    def request_safe_stop(self, reason):
        self.stops.append(reason)

    def close(self):
        self.closed = True


def command(seq, throttle=0, *, epoch="test"):
    return Command(epoch, seq, throttle, 0, 0)


def test_neutral_gate_timeout_latch_and_new_epoch():
    device = Recorder()
    s = ControlSupervisor(device, timeout_s=0.08)
    try:
        s.begin_session("test")
        assert not s.submit(command(1, 1))
        assert s.submit(command(2))
        wait_for(lambda: device.commands)
        assert s.submit(command(3, 0.5))
        wait_for(lambda: len(device.commands) == 2)
        wait_for(lambda: s.state == ControlState.FAILSAFE)
        assert not s.submit(command(4))  # neutral cannot clear a latched failure
        wait_for(lambda: len(device.stops) == 2)
        s.begin_session("replacement")
        assert not s.submit(command(5))
        assert s.submit(command(0, epoch="replacement"))
    finally:
        assert s.close()
    assert device.closed
    assert s.state == ControlState.CLOSED


def test_duplicates_old_and_invalid_values_cannot_renew_deadline():
    s = ControlSupervisor(None, timeout_s=0.08, sequence_bits=16)
    try:
        s.begin_session("test")
        assert s.submit(command(65535))
        assert s.submit(command(0))
        started = time.monotonic()
        while time.monotonic() - started < 0.15:
            assert not s.submit(command(0))
            assert not s.submit(command(65535))
            assert not s.submit(command(1, float("nan")))
            time.sleep(0.005)
        assert s.state == ControlState.FAILSAFE
    finally:
        assert s.close()


@pytest.mark.parametrize("age", [-1, float("nan"), float("inf"), 1])
def test_rejects_invalid_or_expired_age(age):
    s = ControlSupervisor(None, timeout_s=0.2)
    try:
        s.begin_session("test")
        assert not s.submit(command(1), age_s=age)
        assert s.state == ControlState.WAITING_NEUTRAL
    finally:
        assert s.close()


def test_blocked_device_does_not_block_watchdog_or_independent_stop_hook():
    entered, release, safety = threading.Event(), threading.Event(), threading.Event()

    class Blocking(Recorder):
        def send_command(self, *values):
            if values[0]:
                entered.set()
                assert release.wait(2)
            super().send_command(*values)

    def independent_stop(reason):
        if "timed out" in reason or "expired" in reason:
            safety.set()

    device = Blocking()
    s = ControlSupervisor(device, timeout_s=0.08, independent_stop=independent_stop)
    try:
        s.begin_session("test")
        s.submit(command(0))
        wait_for(lambda: device.commands)
        s.submit(command(1, 0.5))
        assert entered.wait(0.5)
        # Keep valid input arriving; the separate device deadline must still trip.
        seq = 2
        while not safety.wait(0.005):
            s.submit(command(seq, 0.8))
            seq += 1
            assert seq < 150
        assert s.state == ControlState.FAILSAFE
        with pytest.raises(RuntimeError, match="previous device"):
            s.begin_session("replacement")
        release.set()
        wait_for(lambda: len(device.stops) >= 2)
        assert (0.8, 0, 0) not in device.commands
    finally:
        release.set()
        assert s.close()


def test_failed_execution_reports_failure_and_latches_stop():
    class Broken(Recorder):
        def send_command(self, *values):
            raise OSError("simulated actuator failure")

    results = []
    s = ControlSupervisor(
        Broken(), timeout_s=0.2, on_result=lambda *args: results.append(args)
    )
    try:
        s.begin_session("test")
        assert s.submit(command(0))
        wait_for(lambda: results)
        assert results[0][1:] == (False, "OSError")
        assert s.state == ControlState.FAILSAFE
    finally:
        assert s.close()


def test_old_session_disconnect_cannot_stop_new_session():
    s = ControlSupervisor(None, timeout_s=0.2)
    try:
        s.begin_session("old")
        s.stop("disconnect", epoch="old")
        wait_for(lambda: s._pending_stop is None and s._device_busy_since is None)
        s.begin_session("new")
        s.stop("delayed old disconnect", epoch="old")
        assert s.state == ControlState.WAITING_NEUTRAL
        assert not s.submit(command(1, epoch="old"))
        assert s.submit(command(0, epoch="new"))
    finally:
        assert s.close()


def test_late_command_cannot_beat_a_delayed_watchdog_tick():
    s = ControlSupervisor(None, timeout_s=0.04)
    try:
        s.begin_session("test")
        assert s.submit(command(0))
        # Simulate a monitor that couldn't run during the input gap.
        with s._condition:
            time.sleep(0.06)
            assert not s.submit(command(1))
            assert s.state == ControlState.FAILSAFE
    finally:
        assert s.close()


def test_queued_command_expires_at_vehicle_window_deadline():
    supervisor = ControlSupervisor(None, timeout_s=1)
    try:
        supervisor.begin_session("test")
        # Hold both workers until a command accepted with 20 ms left expires.
        with supervisor._condition:
            assert supervisor.submit(command(0), deadline=time.monotonic() + 0.02)
            time.sleep(0.04)
            assert not supervisor.submit(command(1), deadline=time.monotonic() + 1)
            assert supervisor.state == ControlState.FAILSAFE
            assert supervisor._pending is None
    finally:
        assert supervisor.close()

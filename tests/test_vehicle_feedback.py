import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rd_core.configuration import load_config
from rd_core.settings import VehicleFeedbackSettings, VehicleSettings
from rd_vehicle.infrastructure.controller import vehicle_feedback
from rd_vehicle.infrastructure.controller.vehicle_feedback import VehicleStatusBuffer


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(ms=1_000_000, mono=100.0)
    monkeypatch.setattr(vehicle_feedback, "time", SimpleNamespace(
        time_ns=lambda: clock.ms * 1_000_000, monotonic=lambda: clock.mono))
    return clock


def message(ms=1_000_000, **changes):
    values = dict(header=SimpleNamespace(stamp=SimpleNamespace(
        sec=ms // 1000, nanosec=ms % 1000 * 1_000_000)),
        cur_speed=3.5, cur_steer=-15.0, left_wheel_speed=1.0,
        right_wheel_speed=-0.5, shift_level=4, is_autodrive=True,
        drive_mode=7, p_status=False)
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize("unit,left_input,right_input", [
    ("m/s", 1.0, -0.5), ("km/h", 3.6, -1.8),
])
def test_all_fields_and_independent_wheel_unit_mapping(clock, unit, left_input, right_input):
    buffer = VehicleStatusBuffer(VehicleFeedbackSettings(wheel_speed_unit=unit))
    assert buffer.receive(message(left_wheel_speed=left_input, right_wheel_speed=right_input))
    sample = buffer.latest()
    assert sample.source_time_ms == clock.ms
    assert sample.received_at == clock.mono
    assert sample.status.speed_mps == 3.5
    assert sample.status.steering_deg == -15.0
    assert sample.status.left_wheel_mps == pytest.approx(1.0)
    assert sample.status.right_wheel_mps == pytest.approx(-0.5)
    assert (sample.status.shift_level, sample.status.is_autodrive,
            sample.status.drive_mode, sample.status.p_status) == (4, True, 7, False)


def test_invalid_fields_do_not_hide_other_measurements(clock):
    buffer = VehicleStatusBuffer(VehicleFeedbackSettings())
    assert buffer.receive(message(left_wheel_speed=float("nan"),
                                  right_wheel_speed=float("inf"), shift_level=256,
                                  is_autodrive=1, p_status="false"))
    status = buffer.latest().status
    assert status.speed_mps == 3.5 and status.steering_deg == -15
    assert status.left_wheel_mps is status.right_wheel_mps is None
    assert status.shift_level is status.is_autodrive is status.p_status is None
    status.validate()


def test_duplicate_old_and_invalid_timestamps_never_refresh_sample(clock):
    buffer = VehicleStatusBuffer(VehicleFeedbackSettings())
    assert buffer.receive(message())
    original = buffer.latest()
    clock.ms += 100
    clock.mono += 0.1
    for ms in (0, 999999, 1_000_000, clock.ms + 100):
        assert not buffer.receive(message(ms))
    assert not buffer.receive(message(header=None))
    assert buffer.latest() is original
    assert buffer.receive(message(clock.ms))
    assert buffer.latest().revision == 2
    buffer.clear()
    assert buffer.latest() is None


def test_queued_feedback_expires_at_original_sample_deadline(clock):
    buffer = VehicleStatusBuffer(VehicleFeedbackSettings())
    assert not buffer.receive(message(clock.ms - 300))
    assert buffer.receive(message(clock.ms - 175))
    sample = buffer.latest()
    assert sample.age_at_receive_ms == 175
    assert sample.age_ms(clock.mono + 0.0625) == 238
    clock.mono += 0.125
    assert buffer.latest() is None


def test_wall_clock_changes_do_not_reset_sample_lifetime(clock):
    buffer = VehicleStatusBuffer(VehicleFeedbackSettings())
    assert buffer.receive(message())
    original = buffer.latest()
    clock.ms += 2_000
    assert buffer.latest() is original
    clock.ms -= 4_000
    assert buffer.latest() is original
    clock.mono += 0.3125
    assert buffer.latest() is None
    assert buffer.receive(message(clock.ms))
    assert buffer.latest().revision == original.revision + 1
    assert not buffer.receive(message(clock.ms))


def test_submillisecond_header_age_is_rounded_up(clock):
    buffer = VehicleStatusBuffer(VehicleFeedbackSettings())
    future = message()
    future.header.stamp.nanosec = 1
    assert not buffer.receive(future)
    current = message(clock.ms - 1)
    current.header.stamp.nanosec += 750_000
    assert buffer.receive(current)
    assert buffer.latest().age_at_receive_ms == 0.25
    assert buffer.latest().age_ms(clock.mono) == 1


@pytest.mark.parametrize("changes", [
    {"wheel_speed_unit": "rpm"}, {"topic": "relative"}, {"topic": "/"},
    {"send_hz": 0}, {"send_hz": 51}, {"stale_timeout_ms": 0},
])
def test_invalid_feedback_configuration(changes):
    with pytest.raises(ValueError):
        replace(VehicleFeedbackSettings(), **changes)


def test_feedback_yaml_is_decoded_without_ros(tmp_path):
    path = tmp_path / "vehicle.yaml"
    path.write_text("feedback:\n  enabled: true\n  wheel_speed_unit: km/h\n  send_hz: 20\n")
    settings = load_config(VehicleSettings, "vehicle", path=path, environ={}).value
    assert settings.feedback.enabled
    assert settings.feedback.wheel_speed_unit == "km/h"
    assert settings.feedback.send_hz == 20


def test_ros_subscription_executor_and_shutdown_use_only_fake_modules(monkeypatch):
    import sys
    import importlib

    events = []
    class Executor:
        def __init__(self, **kwargs):
            self.wakeup = threading.Event()
        def add_node(self, node): events.append("add")
        def spin_once(self, timeout_sec): self.wakeup.wait(timeout_sec)
        def wake(self): self.wakeup.set()
        def remove_node(self, node): events.append("remove")
        def shutdown(self, **kwargs): events.append("shutdown")
    class Node:
        context = object()
        def create_subscription(self, model, topic, callback, depth):
            assert topic == "/vehicle_status"
            events.append("subscribe")
            return object()
        def destroy_subscription(self, subscription): events.append("destroy")
    monkeypatch.setitem(sys.modules, "rclpy.executors", SimpleNamespace(SingleThreadedExecutor=Executor))
    monkeypatch.setitem(sys.modules, "yunle_msgs", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "yunle_msgs.msg", SimpleNamespace(VehicleStatus=object))
    name = "rd_vehicle.infrastructure.controller.ros2_vehicle_feedback"
    module = importlib.import_module(name)
    try:
        source = module.RosVehicleStatusSource(Node(), VehicleFeedbackSettings())
        assert source._thread.is_alive()
        source.close()
        source.close()
        assert not source._thread.is_alive()
        assert events == ["subscribe", "add", "remove", "shutdown", "destroy"]
    finally:
        sys.modules.pop(name, None)

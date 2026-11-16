import math
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
motion = runpy.run_path(
    str(ROOT / "apps/vehicle/src/rd_vehicle/infrastructure/controller/motion.py")
)


@pytest.mark.parametrize(
    "values",
    [
        (math.nan, 0, 0, 5, 2),
        (0, math.inf, 0, 5, 2),
        (2, 0, 0, 5, 2),
        (0, -1, 0, 5, 2),
        (0, 0, 2, 5, 2),
        (0, 0, 0, 0, 2),
        (0, 0, 0, 5, -1),
    ],
)
def test_rejects_invalid_motion(values):
    with pytest.raises(ValueError):
        motion["target_velocity"](*values)


def test_ros_receives_configured_si_limits_and_stop_is_unconfirmed(legacy_python):
    legacy_python(
        "vehicle-unit",
        """
        import sys
        from types import SimpleNamespace
        from dataclasses import replace
        messages = []
        destroyed = []
        class Twist:
            def __init__(self):
                self.linear = SimpleNamespace(x=0.0)
                self.angular = SimpleNamespace(z=0.0)
        class Node:
            def __init__(self, name): pass
            def create_publisher(self, *args):
                return SimpleNamespace(publish=messages.append)
            def destroy_node(self): destroyed.append(True)
        # ROS adapter contract only; no actual rclpy runtime or chassis involved.
        sys.modules['rclpy'] = SimpleNamespace(ok=lambda: True)
        sys.modules['rclpy.node'] = SimpleNamespace(Node=Node)
        sys.modules['geometry_msgs'] = SimpleNamespace()
        sys.modules['geometry_msgs.msg'] = SimpleNamespace(Twist=Twist)
        from rd_vehicle.main import _create_controller
        from rd_core.settings import VehicleSettings, MotionSettings, SerialSettings
        cfg = VehicleSettings(controller='ros2', motion=MotionSettings(1.2, 0.4))
        ctrl = _create_controller(cfg)
        ctrl.send_command(0.5, 0, -0.5)
        assert messages[-1].linear.x == 0.6
        assert messages[-1].angular.z == 0.2
        ctrl.send_command(1, 0.1, 1)
        assert messages[-1].linear.x == messages[-1].angular.z == 0
        result = ctrl.request_safe_stop('test disconnect')
        assert result.requested and not result.confirmed
        assert messages[-1].linear.x == messages[-1].angular.z == 0
        ctrl.close()
        ctrl.close()
        assert destroyed == [True]
        try:
            _create_controller(replace(cfg, controller='typo'))
        except ValueError:
            pass
        else:
            raise AssertionError('unknown controller must not silently disable control')
    """,
    )


def test_serial_timeouts_targets_and_short_writes(legacy_python):
    legacy_python(
        "vehicle-unit",
        """
        from dataclasses import replace
        import serial
        from rd_core.settings import VehicleSettings, MotionSettings, SerialSettings
        from rd_vehicle.infrastructure.controller.serial_controller import SerialController
        opened = {}
        written = []
        class Serial:
            is_open = True
            short_write = False
            def __init__(self, **kwargs): opened.update(kwargs)
            def write(self, data):
                written.append(bytes(data))
                return len(data) - int(self.short_write)
            def close(self): self.is_open = False
        serial.Serial = Serial
        cfg = VehicleSettings(controller='serial', motion=MotionSettings(1.2, 0.4),
                              serial=SerialSettings(read_timeout_s=0.02, write_timeout_s=0.03))
        ctrl = SerialController(cfg)
        assert opened['timeout'] == 0.02
        assert opened['write_timeout'] == 0.03
        ctrl.send_command(0.5, 0, 0.5)
        assert int.from_bytes(written[-1][4:6], 'big') == 60
        assert int.from_bytes(written[-1][10:12], 'big') == 20
        result = ctrl.request_safe_stop('test')
        assert result.requested and not result.confirmed
        assert written[-1][4:6] == written[-1][10:12] == b'\\0\\0'
        ctrl.serial.short_write = True
        try:
            ctrl.send_command(0.5, 0, 0)
        except IOError:
            pass
        else:
            raise AssertionError('partial write accepted')
        ctrl.serial.short_write = False
        ctrl.close()
        assert not ctrl.serial.is_open
        ctrl.close()
    """,
    )

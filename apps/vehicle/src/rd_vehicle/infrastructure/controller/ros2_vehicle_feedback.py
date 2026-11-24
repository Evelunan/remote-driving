"""Owned ROS subscription/executor; imported only when feedback is enabled."""

import threading

from rclpy.executors import SingleThreadedExecutor  # type: ignore
from yunle_msgs.msg import VehicleStatus  # type: ignore
from rd_core.logging import get_logger

from .vehicle_feedback import VehicleStatusBuffer

logger = get_logger(__name__)


class RosVehicleStatusSource(VehicleStatusBuffer):
    def __init__(self, node, settings):
        super().__init__(settings)
        self._node = node
        self._closed = False
        self._stop = threading.Event()
        self._subscription = node.create_subscription(VehicleStatus, settings.topic, self.receive, 10)
        self._executor = None
        try:
            self._executor = SingleThreadedExecutor(context=node.context)
            self._executor.add_node(node)
            self._thread = threading.Thread(target=self._run, name="ros-vehicle-feedback", daemon=True)
            self._thread.start()
        except Exception:
            if self._executor is not None:
                self._executor.shutdown(timeout_sec=0.5)
            node.destroy_subscription(self._subscription)
            raise
        logger.info("ROS chassis feedback subscribed topic=%s speed=m/s wheel_source=%s",
                    settings.topic, settings.wheel_speed_unit)

    def _run(self):
        try:
            while not self._stop.is_set():
                self._executor.spin_once(timeout_sec=0.05)
        except Exception:  # ROS thread boundary; stale state must become unavailable.
            logger.exception("ROS chassis feedback executor stopped")
        finally:
            self.clear()

    def close(self):
        if self._closed:
            return
        self._stop.set()
        self._executor.wake()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            raise RuntimeError("ROS feedback executor did not stop")
        self._executor.remove_node(self._node)
        self._executor.shutdown(timeout_sec=0.5)
        self._node.destroy_subscription(self._subscription)
        self.clear()
        self._closed = True

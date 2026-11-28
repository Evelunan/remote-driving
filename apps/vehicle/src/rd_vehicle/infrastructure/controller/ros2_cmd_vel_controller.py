# ros2_cmd_vel_controller.py
import rclpy  # type: ignore
from geometry_msgs.msg import Twist  # type: ignore
from rclpy.node import Node  # type: ignore
from rd_core.logging import get_logger
from rd_vehicle.interfaces.icontroller import IController, StopResult

from .motion import target_velocity

logger = get_logger(__name__)


class RCLpyCmdVelController(IController):
    def __init__(
        self,
        max_linear=5.0,
        max_angular=2.0,
        *,
        cmd_vel_topic="/cmd_vel",
        feedback=None,
    ):
        target_velocity(0, 0, 0, max_linear, max_angular)
        self._owns_context = not rclpy.ok()
        if not rclpy.ok():
            rclpy.init()
        self.max_linear = max_linear
        self.max_angular = max_angular
        self._closed = False
        self.node = Node("cmd_vel_publisher")
        self.publisher = self.node.create_publisher(Twist, cmd_vel_topic, 10)
        self.feedback = None
        if feedback is not None and feedback.enabled:
            try:
                from .ros2_vehicle_feedback import RosVehicleStatusSource

                self.feedback = RosVehicleStatusSource(self.node, feedback)
            except Exception:
                self.node.destroy_node()
                if self._owns_context and rclpy.ok():
                    rclpy.shutdown()
                raise
        logger.info("CmdVel controller initialized: topic=%s", cmd_vel_topic)

    def send_command(self, throttle, brake, steering):
        msg = Twist()
        v, w = target_velocity(
            throttle,
            brake,
            -steering,  # ros小车是后驱的
            self.max_linear,
            self.max_angular,
        )
        msg.linear.x = v
        # ros小车是后驱的
        msg.angular.z = w
        self.publisher.publish(msg)
        logger.debug("Published target cmd_vel: linear=%s m/s, angular=%s rad/s", v, w)

    def request_safe_stop(self, reason: str) -> StopResult:
        self.send_command(0.0, 1.0, 0.0)
        return StopResult(requested=True, detail=f"zero cmd_vel requested: {reason}")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.request_safe_stop("controller shutdown")
        finally:
            try:
                if self.feedback is not None:
                    self.feedback.close()
            finally:
                self.node.destroy_node()
                if self._owns_context and rclpy.ok():
                    rclpy.shutdown()
        logger.info("Controller closed and resources cleaned up")

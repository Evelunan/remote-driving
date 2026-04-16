# ros2_cmd_vel_controller.py
import rclpy  # type: ignore
from rclpy.node import Node  # type: ignore
from geometry_msgs.msg import Twist  # type: ignore
from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)


class RCLpyCmdVelController(IController):
    def __init__(self, max_linear=5.0, max_angular=2.0):
        if not rclpy.ok():
            rclpy.init()
        self.max_linear = max_linear
        self.max_angular = max_angular
        self.current_v = 0
        self.node = Node("cmd_vel_publisher")
        self.publisher = self.node.create_publisher(Twist, "/cmd_vel", 10)
        logger.info("CmdVel controller initialized")

    def send_command(self, throttle, brake, steering):
        msg = Twist()
        v, w = self._compute_motion(
            throttle,
            brake,
            -steering,  # ros小车是后驱的
            self.current_v,
            self.max_linear,
            self.max_angular,
        )
        msg.linear.x = v
        # ros小车是后驱的
        msg.angular.z = w
        self.publisher.publish(msg)
        logger.info(f"Published cmd_vel: linear={v} m/s, angular={w} rad/s")

    def close(self):
        self.node.destroy_node()
        rclpy.shutdown()
        logger.info("Controller closed and resources cleaned up")

    def _compute_motion(self, throttle, brake, steering, current_v, max_v, max_w):
        v = throttle 
        w = steering * max_w
        # 刹车优先
        if brake > 0:
            if current_v > 0:
                v = max(0.0, current_v - brake * max_v)
            elif current_v < 0:
                v = min(0.0, current_v + brake * max_v)
            else:
                v = 0.0
        else:
            v = throttle * max_v
        return v, w

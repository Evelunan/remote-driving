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
        self.max_linerar = 5.0
        self.max_angulr = 2.0
        self.node = Node("cmd_vel_publisher")
        self.publisher = self.node.create_publisher(Twist, "/cmd_vel", 10)
        logger.info("CmdVel controller initialized")

    def send_command(self, v, w):
        msg = Twist()
        v = float(v * self.max_linerar)
        w = float(v * self.max_linerar)
        msg.linear.x = v
        # ros小车是后驱的
        msg.angular.z = w
        self.publisher.publish(msg)
        logger.info(f"Published cmd_vel: linear={v} m/s, angular={-w} rad/s")

    def close(self):
        self.node.destroy_node()
        rclpy.shutdown()
        logger.info("Controller closed and resources cleaned up")

import serial
from interfaces.icontroller import IController

from config.loaders import VehicleConfig
from logger import get_logger

logger = get_logger(__name__)


class SerialController(IController):
    def __init__(self, config: VehicleConfig):
        # 运动控制参数
        self.max_linear_velocity = config.motion.max_linear_velocity
        self.max_angular_velocity = config.motion.max_angular_velocity

        self.command_template = bytearray(config.communication.protocol.template_bytes)
        # 预分配缓冲区
        self.command_buffer = bytearray(self.command_template)

        # 校验位范围
        self.checksum_start = 2
        self.checksum_end = 11
        self.checksum_position = 12

        self.serial = serial.Serial(
            port=config.communication.serial.port,
            baudrate=config.communication.serial.baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=False,
            xonxoff=False,
            timeout=0.01,
        )

    def send_command(self, v, w):
        """高效发送控制指令"""
        cmd = self._prepare_command(v, w)
        self.serial.write(cmd)
        logger.debug(f"[car] Send command: {cmd}")

    def _prepare_command(self, v, w):
        v = max(min(v, self.max_linear_velocity), -self.max_linear_velocity)
        w = max(min(w, self.max_angular_velocity), -self.max_angular_velocity)

        direction = 0x01 if v < 0 else 0x00
        v_abs = abs(int(v * 100))
        steer_direction = 0x01 if w >= 0 else 0x00
        w_abs = abs(int(w * 100))
        cmd = self.command_buffer
        cmd[3] = direction
        cmd[4] = (v_abs >> 8) & 0xFF
        cmd[5] = v_abs & 0xFF
        cmd[9] = steer_direction
        cmd[10] = (w_abs >> 8) & 0xFF
        cmd[11] = w_abs & 0xFF

        checksum = 0
        for i in range(self.checksum_start, self.checksum_end + 1):
            checksum ^= cmd[i]
        cmd[self.checksum_position] = checksum

        return cmd

    def close(self):
        """关闭串口连接"""
        if self.serial and self.serial.is_open:
            self.serial.close()

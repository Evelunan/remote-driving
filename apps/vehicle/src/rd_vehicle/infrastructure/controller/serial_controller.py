import serial
from rd_core.logging import get_logger
from rd_core.settings import VehicleSettings
from rd_vehicle.interfaces.icontroller import IController, StopResult

from .motion import target_velocity

logger = get_logger(__name__)


class SerialController(IController):
    def __init__(self, config: VehicleSettings):
        # 运动控制参数
        self.max_linear_velocity = config.motion.max_linear_mps
        self.max_angular_velocity = config.motion.max_angular_radps
        if max(self.max_linear_velocity, self.max_angular_velocity) > 655.35:
            raise ValueError("motion limit exceeds the serial uint16 representation")

        self.command_template = bytearray.fromhex(config.serial.template_hex)
        if len(self.command_template) != 13:
            raise ValueError("serial command template must contain 13 bytes")
        # 预分配缓冲区
        self.command_buffer = bytearray(self.command_template)

        # 校验位范围
        self.checksum_start = 2
        self.checksum_end = 11
        self.checksum_position = 12

        self.serial = serial.Serial(
            port=config.serial.port,
            baudrate=config.serial.baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=False,
            xonxoff=False,
            timeout=config.serial.read_timeout_s,
            write_timeout=config.serial.write_timeout_s,
        )

    def send_command(self, throttle, brake, steering):
        """高效发送控制指令"""
        v, w = target_velocity(
            throttle,
            brake,
            steering,
            self.max_linear_velocity,
            self.max_angular_velocity,
        )
        cmd = self._prepare_command(v, w)
        if self.serial.write(cmd) != len(cmd):
            raise OSError("partial serial control write")
        logger.debug(f"[car] Send command: {cmd}")

    def _prepare_command(self, v, w):
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

    def request_safe_stop(self, reason: str) -> StopResult:
        self.send_command(0.0, 1.0, 0.0)
        return StopResult(
            requested=True, detail=f"zero serial target requested: {reason}"
        )

    def close(self):
        """关闭串口连接"""
        if self.serial and self.serial.is_open:
            try:
                self.request_safe_stop("controller shutdown")
            finally:
                self.serial.close()

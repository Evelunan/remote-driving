from rd_driving.interfaces import IAnalogCalculator


class AnalogCalculator(IAnalogCalculator):
    def calculate(self, data: bytes) -> dict:
        throttle_value = data[4] & 0xFF
        return {
            "throttle": self._calc_throttle(throttle_value),
            "brake": self._calc_brake(throttle_value),
            "steering": self._calc_steering(data[3]),
        }

    def _calc_throttle(self, value: int) -> float:
        return (0x80 - value) / 0x80 if value <= 0x80 else 0.0

    def _calc_brake(self, value: int) -> float:
        # 128-255
        return (value - 0x80) / 0x7F if value >= 0x80 else 0.0

    def _calc_steering(self, value: int) -> float:
        if value < 0x80:  # 0-127
            return (value - 0x80) / 0x80  # 左转范围
        elif value > 0x80:  # 129-255
            return (value - 0x80) / 0x7F  # 右转范围
        return 0.0

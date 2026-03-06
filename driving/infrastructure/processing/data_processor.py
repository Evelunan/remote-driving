import json
from typing import Dict
from interfaces.processor import IDataProcessor, IAnalogCalculator
from config.input_config import InputType, InputConfigManager
from logger import get_logger

logger = get_logger(__name__)


class DataProcessor(IDataProcessor):
    def __init__(
        self,
        packet_queue,
        analog_calculator: IAnalogCalculator,
        max_linear: float = 5.0,
        max_angular: float = 30.0,
        max_steering: float = 135.0,
    ):
        self.packet_queue = packet_queue
        self.analog = analog_calculator
        self.configs = InputConfigManager().get_all_configs()

        self.max_linear = max_linear
        self.max_angular = max_angular
        self.max_steering = max_steering

    async def process(self, data: bytes) -> None:
        # 数字输入
        digital_state = self._process_digital(data)

        # 模拟输入
        analog_values = self.analog.calculate(data)
        throttle = analog_values["throttle"]
        brake = analog_values["brake"]
        steering = analog_values["steering"]

        # 限幅
        steering = self._clamp(steering, -self.max_steering, self.max_steering)
        throttle = self._clamp(throttle, 0.0, 1.0)
        brake = self._clamp(brake, 0.0, 1.0)

        # 控制量
        linear = self._calc_linear(throttle, brake)
        angular = self._calc_angular(steering)

        packet = self._create_packet(linear, angular)
        if packet is not None:
            await self.packet_queue.put(packet)

    def _process_digital(self, data: bytes) -> Dict[InputType, bool]:
        state = {}
        for config in self.configs.values():
            active = True
            for i, pos in enumerate(config.positions):
                value = data[pos] & 0xFF
                if config.min_values[i] <= value <= config.max_values[i]:
                    continue
                active = False
                break
            state[config.input_type] = active
        return state

    def _calc_linear(self, throttle: float, brake: float) -> float:
        return (throttle - brake) * self.max_linear

    def _calc_angular(self, steering: float) -> float:
        ratio = self.max_angular / self.max_steering
        return steering * ratio

    def _create_packet(self, linear: float, angular: float):
        if linear == 0 and angular == 0:
            return None
        return json.dumps(
            {
                "linear": linear,
                "angular": angular,
            }
        )

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        return max(min_val, min(max_val, value))

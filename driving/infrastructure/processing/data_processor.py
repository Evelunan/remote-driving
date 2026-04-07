import json
from typing import Dict
from interfaces import IDataProcessor, IAnalogCalculator, IMessageQueue
from config.input_config import InputType, InputConfigManager
from infrastructure.communication import ControlState

from logger import get_logger

logger = get_logger(__name__)


class DataProcessor(IDataProcessor):
    def __init__(
        self,
        processed_queue: IMessageQueue,
        analog_calculator: IAnalogCalculator,
    ):
        self.processed_queue = processed_queue
        self.analog = analog_calculator
        self.configs = InputConfigManager().get_all_configs()
        self.last_v = None
        self.last_w = None

    async def process(self, data: bytes) -> None:
        # 数字输入
        digital_state = self._process_digital(data)
        # 模拟输入
        # analog_values = self.analog.calculate(data)
        # throttle = analog_values["throttle"]
        # brake = analog_values["brake"]
        # steering = analog_values["steering"]

        # v = self._clamp(throttle - brake, -1.0, 1.0)
        # w = self._clamp(steering, -1.0, 1.0)
        # v = round(v, 2)
        # w = round(w, 2)
        v = data[4]
        w = data[3]

        state = ControlState(v, w)
        await self.processed_queue.put(state)

        # logger.info(f" v_raw:{data[4]}, w_raw:{data[3]}")
        # logger.info(f"v: {v}, w: {w}")

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

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        return max(min_val, min(max_val, value))

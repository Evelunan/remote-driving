import json
from typing import Dict
from interfaces.processor import IDataProcessor, IAnalogCalculator
from config.input_config import InputType, InputConfigManager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..communication import ControlState

from logger import get_logger

logger = get_logger(__name__)


class DataProcessor(IDataProcessor):
    def __init__(
        self,
        control_state: "ControlState",
        analog_calculator: IAnalogCalculator,
    ):
        self.control_state = control_state
        self.analog = analog_calculator
        self.configs = InputConfigManager().get_all_configs()
        self.last_v = 0.0
        self.last_w = 0.0

    def process(self, data: bytes) -> None:
        # 数字输入
        digital_state = self._process_digital(data)
        # 模拟输入
        analog_values = self.analog.calculate(data)
        throttle = analog_values["throttle"]
        brake = analog_values["brake"]
        steering = analog_values["steering"]

        v = self._clamp(throttle - brake, -1.0, 1.0)
        w = self._clamp(steering, -1.0, 1.0)
        v = round(v, 2)
        w = round(w, 2)

        if v != self.last_v or w != self.last_w:
            self.last_v = v
            self.last_w = w
            self.control_state.update(v, w)
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

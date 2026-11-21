from typing import Dict, NamedTuple
from rd_core.samples import RawInput, ControlInput
from rd_driving.interfaces import IDataProcessor, IAnalogCalculator, IMessageQueue
from rd_driving.infrastructure.device import InputConfigManager, InputType

from rd_core.logging import get_logger

logger = get_logger(__name__)


class LocalInputState(NamedTuple):
    reverse: bool
    braking: bool


class DataProcessor(IDataProcessor):
    def __init__(
        self,
        processed_queue: IMessageQueue,
        analog_calculator: IAnalogCalculator,
    ):
        self.processed_queue = processed_queue
        self.analog = analog_calculator
        self.configs = InputConfigManager().get_all_configs()
        self._reverse = False
        self._latest: ControlInput | None = None

    def local_input_state(self, timeout_s: float) -> LocalInputState | None:
        if self._latest is None or not self._latest.fresh(timeout_s):
            return None
        return LocalInputState(self._reverse, self._latest.brake > 0)

    async def process(self, sample: RawInput) -> None:
        data = sample.data
        if data is None or len(data) < 5:
            self._latest = None
            await self.processed_queue.put(ControlInput(
                0, 0, 0, sample.sampled_at, sample.timestamp_ms, valid=False,
            ))
            return
        digital_state = self._process_digital(data)
        gear_up = digital_state[InputType.GEAR_UP]
        gear_down = digital_state[InputType.GEAR_DOWN]
        if gear_up != gear_down:
            self._reverse = gear_down

        analog_values = self.analog.calculate(data)
        if analog_values["brake"] > 0:
            analog_values["throttle"] = 0.0
        elif self._reverse and analog_values["throttle"] > 0:
            analog_values["throttle"] = -analog_values["throttle"]

        self._latest = ControlInput(
            **analog_values, sampled_at=sample.sampled_at, timestamp_ms=sample.timestamp_ms,
        )
        await self.processed_queue.put(self._latest)

    def _process_digital(self, data: bytes) -> Dict[InputType, bool]:
        state = {}
        for config in self.configs.values():
            active = True
            for i, pos in enumerate(config.positions):
                if pos >= len(data):
                    active = False
                    break
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

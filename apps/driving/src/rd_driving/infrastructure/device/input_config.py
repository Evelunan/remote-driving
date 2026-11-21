from enum import Enum
from typing import Dict, List, Tuple, Union

class InputType(Enum):
    LS = 1
    RS = 2
    LT = 3
    RT = 4
    Y = 5
    X = 6
    B = 7
    A = 8
    SELECT = 9
    START = 10
    PS = 11
    DPAD_UP = 12
    DPAD_DOWN = 13
    DPAD_LEFT = 14
    DPAD_RIGHT = 15
    GEAR_UP = 16
    GEAR_DOWN = 17

class InputConfig:
    def __init__(self, input_type: InputType, positions: List[int], 
                 expected_values: List[int] = None, 
                 min_values: List[int] = None, max_values: List[int] = None):
        self.input_type = input_type
        self.positions = positions
        
        if expected_values is not None:
            # 精确值检查模式
            self.expected_values = expected_values
            self.is_range = [False] * len(positions)
            self.min_values = expected_values.copy()
            self.max_values = expected_values.copy()
        else:
            # 范围检查模式
            self.min_values = min_values
            self.max_values = max_values
            self.is_range = [True] * len(positions)
            self.expected_values = [0] * len(positions)  # 无效占位符

class InputConfigManager:
    def __init__(self):
        self.configs: Dict[InputType, InputConfig] = {}
        
        # 按钮配置
        self.configs[InputType.LS] = InputConfig(
            InputType.LS, [1], expected_values=[0x04]
        )
        self.configs[InputType.RS] = InputConfig(
            InputType.RS, [1], expected_values=[0x08]
        )
        # LT、RT 和挡杆各有独立字节：按下为 0xFF，松开为 0x00。
        self.configs[InputType.LT] = InputConfig(
            InputType.LT, [17], expected_values=[0xFF]
        )
        self.configs[InputType.RT] = InputConfig(
            InputType.RT, [18], expected_values=[0xFF]
        )
        self.configs[InputType.Y] = InputConfig(
            InputType.Y, [0, 11], expected_values=[0x01, 0xFF]
        )
        self.configs[InputType.X] = InputConfig(
            InputType.X, [0, 14], expected_values=[0x08, 0xFF]
        )
        self.configs[InputType.B] = InputConfig(
            InputType.B, [0, 12], expected_values=[0x02, 0xFF]
        )
        self.configs[InputType.A] = InputConfig(
            InputType.A, [0, 13], expected_values=[0x04, 0xFF]
        )
        self.configs[InputType.SELECT] = InputConfig(
            InputType.SELECT, [1], expected_values=[0x01]
        )
        self.configs[InputType.START] = InputConfig(
            InputType.START, [1], expected_values=[0x02]
        )
        self.configs[InputType.PS] = InputConfig(
            InputType.PS, [1], expected_values=[0x10]
        )
        
        # 方向键配置
        self.configs[InputType.DPAD_UP] = InputConfig(
            InputType.DPAD_UP, [2, 9], min_values=[0x00, 0x00], max_values=[0x00, 0xFF]
        )
        self.configs[InputType.DPAD_DOWN] = InputConfig(
            InputType.DPAD_DOWN, [2, 10], min_values=[0x00, 0x04], max_values=[0x04, 0xFF]
        )
        self.configs[InputType.DPAD_LEFT] = InputConfig(
            InputType.DPAD_LEFT, [2, 8], min_values=[0x00, 0x06], max_values=[0x06, 0xFF]
        )
        self.configs[InputType.DPAD_RIGHT] = InputConfig(
            InputType.DPAD_RIGHT, [2, 7], min_values=[0x00, 0x02], max_values=[0x02, 0xFF]
        )
        
        # 挡杆配置
        self.configs[InputType.GEAR_UP] = InputConfig(
            InputType.GEAR_UP, [16], expected_values=[0xFF]
        )
        self.configs[InputType.GEAR_DOWN] = InputConfig(
            InputType.GEAR_DOWN, [15], expected_values=[0xFF]
        )

    def get_config(self, input_type: InputType) -> InputConfig:
        return self.configs.get(input_type, None)
    
    def get_all_configs(self) -> Dict[InputType, InputConfig]:
        return self.configs.copy()

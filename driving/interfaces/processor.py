from abc import ABC, abstractmethod


class IDataProcessor(ABC):
    """数据处理器接口"""

    @abstractmethod
    async def process(self, data: bytes) -> None:
        """处理原始数据"""
        pass


class IAnalogCalculator(ABC):
    """模拟信号计算器接口"""

    @abstractmethod
    def calculate(self, data: bytes) -> dict:
        """计算模拟信号值，返回 {throttle, brake, steering}"""
        pass

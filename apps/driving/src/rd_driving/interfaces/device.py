from abc import ABC, abstractmethod
from typing import Callable, Awaitable
from rd_core.samples import RawInput


class IInputDevice(ABC):
    """输入设备接口"""

    @abstractmethod
    async def connect(self) -> None:
        """连接设备"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """断开设备"""
        pass

    @abstractmethod
    def set_data_callback(
        self, callback: Callable[[RawInput], None | Awaitable[None]]
    ) -> None:
        """设置数据回调"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        pass

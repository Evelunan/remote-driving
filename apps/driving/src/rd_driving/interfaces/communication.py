from abc import ABC, abstractmethod
from typing import Any


class IMessageQueue(ABC):
    """消息队列接口"""

    @abstractmethod
    async def put(self, item: Any) -> None:
        """放入消息"""
        pass

    @abstractmethod
    async def get(self) -> Any:
        """获取消息"""
        pass

    @abstractmethod
    def is_empty(self) -> bool:
        """检查队列是否为空"""
        pass

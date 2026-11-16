import asyncio
from typing import Any

from rd_driving.interfaces import IMessageQueue


# 只保留最新一条数据是否会更好？
class LatestValueQueue(IMessageQueue):
    def __init__(self):
        self._value = None
        self._condition = asyncio.Condition()

    async def put(self, item: Any) -> None:
        async with self._condition:
            self._value = item
            self._condition.notify()

    async def get(self) -> Any:
        async with self._condition:
            while self._value is None:
                await self._condition.wait()
            value = self._value
            self._value = None
            return value

    async def is_empty(self) -> bool:
        async with self._condition:
            return self._value == None

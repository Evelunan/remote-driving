import asyncio
from collections import deque
from typing import Deque, Any
from interfaces import IMessageQueue


class AsyncQueue(IMessageQueue):
    def __init__(self, max_size: int = 100):
        self._queue: Deque[Any] = deque(maxlen=max_size)
        self._condition = asyncio.Condition()

    async def put(self, item: Any) -> None:
        async with self._condition:
            self._queue.append(item)
            self._condition.notify()

    async def get(self) -> Any:
        async with self._condition:
            while not self._queue:
                await self._condition.wait()
            return self._queue.popleft()

    async def is_empty(self) -> bool:
        async with self._condition:
            return len(self._queue) == 0


# 只保留最新一条数据是否会更好？
# class LatestValueQueue(IMessageQueue):
#     def __init__(self):
#         self._value = None
#         self._condition = asyncio.Condition()

#     async def put(self, item: Any) -> None:
#         async with self._condition:
#             self._value = item
#             self._condition.notify()

#     async def get(self) -> Any:
#         async with self._condition:
#             while self._value is None:
#                 await self._condition.wait()
#             value = self._value
#             self._value = None
#             return value

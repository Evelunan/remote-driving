import asyncio
from collections import deque
from typing import Deque, Any
from rd_driving.interfaces import IMessageQueue


class AsyncQueue(IMessageQueue):
    def __init__(self, max_size: int = 10):
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

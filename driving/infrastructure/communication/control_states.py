from dataclasses import dataclass
import asyncio
import time


@dataclass
class ControlState:

    linear: float = 0.0
    angular: float = 0.0
    timestamp: float = 0.0
    lock: asyncio.Lock = asyncio.Lock()

    async def update(self, linear, angular):

        async with self.lock:
            self.linear = linear
            self.angular = angular
            self.timestamp = time.monotonic()

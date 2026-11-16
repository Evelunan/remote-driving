# signaling_client.py
import json
import websockets
from typing import Callable, Dict, Any


class SignalingClient:
    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.handlers: Dict[str, Callable] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.url)

    async def send(self, data: dict):
        await self.ws.send(json.dumps(data))

    def on(self, msg_type: str, handler: Callable):
        self.handlers[msg_type] = handler

    async def listen(self):
        async for msg in self.ws:
            data = json.loads(msg)
            handler = self.handlers.get(data.get("type"))
            if handler:
                await handler(data)

    async def close(self):
        if self.ws:
            await self.ws.close()
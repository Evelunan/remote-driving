# signaling_client.py
import json
import websockets
from typing import Callable
from rd_core.logging import get_logger

logger = get_logger(__name__)


class SignalingClient:
    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.connected = False

    async def connect(self):
        try:
            self.ws = await websockets.connect(self.url)
            self.connected = True
            logger.info(f"连接信令服务器：{self.url}")
        except Exception as e:
            logger.exception(f"信令服务器{self.url} 连接失败: {e}")
            raise

    async def send(self, data: dict):
        if self.ws:
            await self.ws.send(json.dumps(data))

    async def recv(self) -> dict:
        """只接收一条消息（核心优化）"""
        if self.ws:
            msg = await self.ws.recv()
            return json.loads(msg)
        raise RuntimeError("WebSocket not connected")

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None
        self.connected = False
        logger.info("信令服务器已断开连接")

    def is_connected(self):
        return self.connected and self.ws is not None

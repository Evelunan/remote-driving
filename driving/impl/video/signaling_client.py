import json
import asyncio
import websockets
from typing import Optional, Any

from interfaces import ISignalingClient
from logger import get_logger

logger = get_logger(__name__)


class WebSocketSignalingClient(ISignalingClient):
    """生产级 WebSocket 信令客户端"""

    def __init__(self, server_url: str, room: str):
        self._server_url = server_url
        self._room = room
        self._ws: Optional[Any] = None
        self._connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            self._ws = await websockets.connect(
                self._server_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )

            self._connected = True
            await self.send({"type": "join", "room": self._room})

            logger.info(f"信令服务器连接成功: {self._server_url}, 房间: {self._room}")

        except Exception as e:
            self._connected = False
            logger.exception("信令服务器连接失败")
            raise

    async def disconnect(self) -> None:
        async with self._lock:
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass

                self._ws = None

            self._connected = False
            logger.info("信令服务器已断开连接")

    async def send(self, message: dict) -> None:
        async with self._lock:
            if not self.is_connected():
                raise RuntimeError("WebSocket not connected")

            try:
                await self._ws.send(json.dumps(message))
            except Exception:
                self._connected = False
                logger.exception("发送信令失败")
                raise

    async def receive(self, timeout: Optional[float] = None) -> dict:
        if not self.is_connected():
            raise RuntimeError("WebSocket not connected")

        try:
            if timeout:
                raw = await asyncio.wait_for(self._ws.recv(), timeout)
            else:
                raw = await self._ws.recv()

            return json.loads(raw)

        except asyncio.TimeoutError:
            raise

        except Exception:
            self._connected = False
            logger.exception("接收信令失败")
            raise

    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

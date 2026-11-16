"""
对等端模型

代表一个 WebSocket 连接，包含连接信息和操作方法
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import json
from websockets.exceptions import ConnectionClosed

from rd_core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Peer:
    """
    对等端，代表一个 WebSocket 连接

    属性:
        id: 唯一标识
        websocket: WebSocket 连接
        room_id: 所在房间ID
        connected_at: 连接时间
    """

    id: str
    websocket: Any
    room_id: str | None = None
    connected_at: datetime = field(default_factory=datetime.now)

    async def send(self, message: dict[str, Any]) -> bool:
        """
        发送消息给此对等端

        返回: 是否发送成功
        """
        try:
            await self.websocket.send(json.dumps(message))
            return True
        except ConnectionClosed:
            logger.warning(
                f"无法向对等端 {self.id} 发送消息：连接已关闭（房间 ID：{self.room_id}）"
            )
            return False
        except Exception as e:
            logger.error(f"向对等端 {self.id} 发送消息时发生错误：{e}", exc_info=True)
            return False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """安全地关闭 WebSocket 连接"""
        if not self.websocket or getattr(self.websocket, "closed", False):
            logger.debug(f"对等端 {self.id} 的连接已关闭或 WebSocket 无效")
            return

        try:
            await self.websocket.close(code=code, reason=reason)
            logger.debug(
                f"对等端 {self.id} 的连接已关闭（状态码：{code}，原因：'{reason}'）"
            )
        except ConnectionClosed:
            logger.debug(f"尝试关闭对等端 {self.id} 时发现连接早已断开")
        except Exception as e:
            logger.warning(f"关闭对等端 {self.id} 的连接失败：{e}", exc_info=True)


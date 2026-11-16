"""
数据模型模块

包含所有数据模型定义：
- Peer: WebSocket 对等端模型
- Room: 信令房间模型
"""

from .peer import Peer
from .room import Room

__all__ = ["Peer", "Room"]

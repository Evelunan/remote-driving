"""
房间模型

代表一个信令房间，包含多个对等端和房间管理方法
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .peer import Peer


@dataclass
class Room:
    """
    信令房间，包含多个对等端

    属性:
        id: 房间ID
        max_peers: 最大人数
        peers: 房间内的对等端字典
    """

    id: str
    max_peers: int = 2
    create_at: datetime = field(default_factory=datetime.now)
    peers: dict[str, Peer] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """房间是否为空"""
        return len(self.peers) == 0

    @property
    def is_full(self) -> bool:
        """房间是否已满"""
        return len(self.peers) >= self.max_peers

    @property
    def peer_count(self) -> int:
        """当前人数"""
        return len(self.peers)

    def add_peer(self, peer: Peer) -> bool:
        """添加对等端，满了返回False"""
        if self.is_full:
            return False
        peer.room_id = self.id
        self.peers[peer.id] = peer
        return True

    def remove_peer(self, peer_id: str) -> Optional[Peer]:
        """移除对等端"""
        peer = self.peers.pop(peer_id, None)
        if peer:
            peer.room_id = None
        return peer

    def get_other_peer_ids(self, exclude_id: str) -> list[str]:
        """获取除指定ID外的其他对等端ID"""
        return [pid for pid in self.peers if pid != exclude_id]

    async def broadcast(
        self, message: dict[str, Any], exclude_id: Optional[str] = None
    ) -> int:
        """
        广播消息给房间内所有人

        参数:
            message: 要发送的消息
            exclude_id: 要排除的对等端ID

        返回: 成功发送的数量
        """
        count = 0
        for peer_id, peer in self.peers.items():
            if peer_id == exclude_id:
                continue
            if await peer.send(message):
                count += 1
        return count


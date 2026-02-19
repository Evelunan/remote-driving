"""
房间管理服务

负责管理所有房间和对等端，处理加入/离开/消息路由等业务逻辑
"""

from dataclasses import dataclass, field
from typing import Any, Optional
import uuid
import json

from logger import get_logger
from models import Peer, Room

logger = get_logger(__name__)


@dataclass
class RoomManager:
    """
    房间管理器

    负责管理所有房间和对等端，处理加入/离开/消息路由
    """

    max_peers_per_room: int = 2
    rooms: dict[str, Room] = field(default_factory=dict)
    peers: dict[str, Peer] = field(default_factory=dict)

    # -------------------- 房间操作 --------------------

    def get_room(self, room_id: str) -> Optional[Room]:
        """获取房间"""
        return self.rooms.get(room_id)

    def get_or_create_room(self, room_id: str) -> Room:
        """获取或创建房间"""
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(
                id=room_id,
                max_peers=self.max_peers_per_room,
            )
            logger.info(f"房间已创建: {room_id}")
        return self.rooms[room_id]

    def remove_room_if_empty(self, room_id: str) -> None:
        """如果房间为空则删除"""
        room = self.rooms.get(room_id)
        if room and room.is_empty:
            del self.rooms[room_id]
            logger.info(f"房间已销毁: {room_id}")

    # -------------------- 对等端操作 --------------------
    async def register_peer(self, websocket) -> Peer:
        """注册新的对等端"""
        peer_id = str(uuid.uuid4())
        peer = Peer(id=peer_id, websocket=websocket)
        self.peers[peer_id] = peer
        logger.info(f"对等端已连接: {peer_id}")
        return peer

    async def unregister_peer(self, peer: Peer) -> None:
        """注销对等端"""
        if peer.room_id:
            await self.leave_room(peer)
        self.peers.pop(peer.id, None)
        logger.info(f"对等端已断开: {peer.id}")

    async def join_room(self, peer: Peer, room_id: str) -> bool:
        """
        加入房间

        返回: 是否成功加入
        """
        room = self.get_or_create_room(room_id)

        if room.is_full:
            await peer.send({"type": "full", "message": "room is full"})
            logger.info(f"对等端 {peer.id} 加入被拒绝 - 房间 {room_id} 已满")
            return False

        await room.broadcast({"type": "new_peer", "id": peer.id})
        room.add_peer(peer)

        existing_peer = room.get_other_peer_ids(peer.id)

        await peer.send({"type": "joined", "id": peer.id, "peers": existing_peer})
        logger.info(f"[{room_id}] {peer.id} 加入 ({room.peer_count}/{room.max_peers})")
        return True

    async def leave_room(self, peer: Peer) -> None:
        """离开房间"""
        if not peer.room_id:
            return

        room_id = peer.room_id
        room = self.get_room(room_id)

        if not room:
            peer.room_id = None
            return

        room.remove_peer(peer.id)

        await room.broadcast({"type": "peer_left", "id": peer.id})
        logger.info(f"[{room_id}] {peer.id} 离开 ({room.peer_count}/{room.max_peers})")

        self.remove_room_if_empty(room_id)

    async def handle_message(self, peer: Peer, raw_message: str) -> None:
        """处理收到的消息"""
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            await peer.send({"type": "error", "message": "无效的JSON格式"})
            return

        msg_type = message.get("type")
        if not msg_type:
            return

        # 查找并执行处理器
        handler_method = {
            "join": self.handle_join,
            "offer": self.handle_forward,
            "answer": self.handle_forward,
            "candidate": self.handle_forward,
        }.get(msg_type)
        if handler_method is not None:
            await handler_method(peer, message)
        else:
            logger.warning(f"未知消息类型: {msg_type}")

    async def handle_join(self, peer: Peer, message: dict[str, Any]):
        """
        处理加入房间请求

        消息格式: {"type": "join", "room": "<room_id>"}
        """

        room_id = message.get("room")
        if not room_id:
            await peer.send({"type": "error", "message": "invalid_room_id"})
            return

        if peer.room_id:
            await self.leave_room(peer)

        await self.join_room(peer, room_id)

    async def handle_forward(self, peer: Peer, message: dict[str, Any]):
        """
        转发消息处理器

        用于转发 offer/answer/candidate 等 WebRTC 信令消息
        支持定向发送(带to字段)和广播
        """

        if not peer.room_id:
            return

        room = self.get_room(peer.room_id)
        if not room:
            return

        if "from" not in message:
            message["from"] = peer.id

        target_id = message.get("to")
        if target_id and target_id in room.peers:
            await room.peers[target_id].send(message)
        else:
            await room.broadcast(message, peer.id)


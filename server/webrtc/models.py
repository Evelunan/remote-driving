# models.py
from dataclasses import dataclass, field
from datetime import datetime
import uuid
import json
from typing import Any, Optional
from websockets.exceptions import ConnectionClosed

from logger import get_logger

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
    room_id: Optional[str] = None
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
        return self.rooms[room_id]

    def get_or_create_room(self, room_id: str) -> Room:
        """获取或创建房间"""
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(
                id=room_id,
                max_peers=self.max_peers_per_room,
            )
            logger.info(f"房间已创建: {room_id}")
        return self.rooms[room_id]

    def remove_room_if_empty(self, room_id) -> None:
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
            self.leave_room(peer)

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

# models.py
from dataclasses import dataclass, field
from datetime import datetime
import uuid
import json
from typing import Any
from websockets.exceptions import ConnectionClosed

from logger import get_logger

logger = get_logger(__name__)


@dataclass
class Peer:
    id: str
    websocket: Any
    room_id: str | None
    connected_at: datetime = field(default_factory=datetime.now)

    async def send(self, message: dict[str, Any]) -> bool:
        """向该对等端发送消息"""
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

    id: str
    max_peers: int = 2
    create_at: datetime = field(default_factory=datetime.now)
    peers: dict[str, Peer] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:

        return len(self.peers) == 0

    @property
    def is_full(self) -> bool:
        return len(self.peers) >= self.max_peers

    @property
    def peer_count(self) -> int:
        return len(self.peers)

    def add_peer(self, peer: Peer) -> bool:
        if self.is_full:
            return False
        peer.room_id = self.id
        self.peers[peer.id] = peer
        return True

    def remove_peer(self, peer_id: str):
        peer = self.peers.pop(peer_id, None)
        if peer:
            peer.room_id = None
        return peer

    def get_other_peer_ids(self, exclude_id: str) -> list[str]:
        return [pid for pid in self.peers if pid != exclude_id]

    async def broadcast(self, message: dict[str, Any], exclude_id: str | None) -> int:
        count = 0
        for peer_id, peer in self.peers.items():
            if peer_id == exclude_id:
                continue
            if await peer.send(message):
                count += 1
        return count


@dataclass
class RoomManager:
    max_peers_per_room: int = 2
    rooms: dict[str, Room] = field(default_factory=dict)
    peers: dict[str, Peer] = field(default_factory=dict)

    def get_room(self, room_id: str) -> Room | None:
        return self.rooms[room_id]

    def get_or_create_room(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(
                id=room_id,
                max_peers=self.max_peers_per_room,
            )
            logger.info(f"房间已创建: {room_id}")
        return self.rooms[room_id]

    def remove_room_if_empty(self, room_id):
        room = self.rooms.get(room_id)
        if room and room.is_empty:
            del self.rooms[room_id]
            logger.info(f"房间已销毁: {room_id}")

    async def register_peer(self, websocket) -> Peer:
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
        room = self.get_or_create_room(room_id)

        if room.is_full:
            await peer.send({"type": "full", "message": "room is full"})
            logger.info(f"对等端 {peer.id} 加入被拒绝 - 房间 {room_id} 已满")

        await room.broadcast({"type": "new_peer", "id": peer.id})
        room.add_peer(peer)

        existing_peer = room.get_other_peer_ids(peer.id)

        await peer.send({"type": "joined", "id": peer.id, "peers": existing_peer})
        return True

    async def leave_room(self, peer: Peer) -> None:
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
        room_id = message.get("room")
        if not room_id:
            await peer.send({"type": "error", "message": "invalid_room_id"})
            return

        if peer.room_id:
            self.leave_room(peer)

        await self.join_room(peer, room_id)

    async def handle_forward(self, peer: Peer, message: dict[str, Any]):
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

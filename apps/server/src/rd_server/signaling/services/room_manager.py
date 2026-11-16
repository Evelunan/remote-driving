"""Room-scoped routing; connection identity never comes from a message body."""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from rd_core.auth import AuthorizationError, Principal

from rd_server.signaling.models import Peer, Room


@dataclass
class RoomManager:
    max_peers_per_room: int = 2
    send_timeout_s: float = 3.0
    rooms: dict[str, Room] = field(default_factory=dict)
    peers: dict[str, Peer] = field(default_factory=dict)

    async def register_peer(
        self, websocket, principal: Principal | None = None
    ) -> Peer:
        peer = Peer(
            str(uuid.uuid4()), websocket, principal, send_timeout_s=self.send_timeout_s
        )
        self.peers[peer.id] = peer
        return peer

    async def unregister_peer(self, peer: Peer) -> None:
        await self.leave_room(peer)
        self.peers.pop(peer.id, None)

    async def join_room(self, peer: Peer, room_id: str) -> bool:
        if peer.principal is not None:
            peer.principal.check_room(room_id)
        if peer.room_id == room_id:
            room = self.rooms[room_id]
            await peer.send(
                {
                    "type": "joined",
                    "id": peer.id,
                    "peers": room.get_other_peer_ids(peer.id),
                }
            )
            return True
        if peer.room_id is not None:
            await self.leave_room(peer)
        room = self.rooms.setdefault(
            room_id, Room(id=room_id, max_peers=self.max_peers_per_room)
        )
        # Capacity check and membership insertion contain no suspension point.
        if not room.add_peer(peer):
            await peer.send({"type": "full", "message": "room is full"})
            return False
        await peer.send(
            {"type": "joined", "id": peer.id, "peers": room.get_other_peer_ids(peer.id)}
        )
        await room.broadcast({"type": "new_peer", "id": peer.id}, peer.id)
        return True

    async def leave_room(self, peer: Peer) -> None:
        room = self.rooms.get(peer.room_id)
        if room is None:
            peer.room_id = None
            return
        room.remove_peer(peer.id)
        if room.is_empty:
            self.rooms.pop(room.id, None)
        await room.broadcast({"type": "peer_left", "id": peer.id})

    async def handle_message(self, peer: Peer, raw_message: str) -> None:
        try:
            if peer.principal is not None:
                peer.principal.check_active()
            try:
                message = json.loads(raw_message)
            except (ValueError, TypeError, RecursionError):
                await self.error(peer, "invalid_json")
                return
            if not isinstance(message, dict) or not isinstance(
                message.get("type"), str
            ):
                await self.error(peer, "invalid_message")
                return
            handler = {
                "join": self.handle_join,
                "offer": self.handle_forward,
                "answer": self.handle_forward,
                "candidate": self.handle_forward,
            }.get(message["type"])
            if handler is None:
                await self.error(peer, "unknown_message")
            else:
                await handler(peer, message)
        except AuthorizationError:
            await peer.close(1008, "access denied")

    async def error(self, peer: Peer, reason: str):
        await peer.send({"type": "error", "message": reason})

    async def handle_join(self, peer: Peer, message: dict[str, Any]):
        room_id = message.get("room")
        if not isinstance(room_id, str) or not 1 <= len(room_id) <= 128:
            await self.error(peer, "invalid_room_id")
            return
        await self.join_room(peer, room_id)

    async def handle_forward(self, peer: Peer, message: dict[str, Any]):
        room = self.rooms.get(peer.room_id)
        if room is None or room.peers.get(peer.id) is not peer:
            await self.error(peer, "join_required")
            return
        if peer.principal is not None:
            peer.principal.check_room(room.id)
        forwarded = {**message, "from": peer.id}
        if "to" in message:
            target_id = message["to"]
            if (
                not isinstance(target_id, str)
                or target_id == peer.id
                or target_id not in room.peers
            ):
                await self.error(peer, "invalid_target")
                return
            await room.peers[target_id].send(forwarded)
        else:
            await room.broadcast(forwarded, peer.id)

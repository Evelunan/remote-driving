"""Authenticated media receiver with owned, generation-scoped decode tasks."""

import asyncio

from aiortc import RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from rd_core.video_safety import VideoSource
from rd_webrtc.session import MediaSession


class VideoStatus:
    CONNECTING = "connecting"
    JOINED = "joined"
    WAITING_PEER = "waiting_peer"
    PEER_JOINED = "peer_joined"
    RECEIVING_OFFER = "receiving_offer"
    CONNECTED = "connected"
    RECEIVING = "receiving"
    PEER_LEFT = "peer_left"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class WebRTCReceiver(MediaSession):
    def __init__(
        self, room, network, credentials, *, views=("front", "back", "left", "right")
    ):
        super().__init__(room, network, credentials)
        self._on_frame = None
        self._on_invalidate = None
        self._views = frozenset(views)
        self._publishers = {}
        self._track_ids = {}
        self.signaling.on("offer", self._on_offer)
        self.signaling.on("new_peer", self._on_new_peer)

    def on_frame(self, callback):
        self._on_frame = callback

    def on_invalidate(self, callback):
        self._on_invalidate = callback

    async def _on_joined(self, data):
        self._publishers = {
            peer: VideoSource.from_message(peer, data.get("peer_sources", {}).get(peer))
            for peer, role in data.get("peer_roles", {}).items()
            if role == "vehicle"
        }
        if self._publishers:
            self._emit_status("peer_joined", "车端在线，等待视频请求")
        else:
            self._emit_status("waiting_peer", "等待车端连接")

    async def _on_new_peer(self, data):
        if data.get("role") == "vehicle":
            self._publishers[data["id"]] = VideoSource.from_message(
                data["id"], data.get("source")
            )
            self._emit_status("peer_joined", "车端在线，等待视频请求")

    async def _create_peer(self, peer_id):
        peer = await super()._create_peer(peer_id)
        self._track_ids[peer] = set()

        @peer.pc.on("track")
        def on_track(track):
            if (
                not self._current(peer_id, peer)
                or track.kind != "video"
                or track.id not in self._views
            ):
                track.stop()
                return
            if track.id in self._track_ids[peer]:
                track.stop()
                self._schedule_cleanup(peer_id, peer)
                return
            self._track_ids[peer].add(track.id)
            self._spawn_peer_task(peer_id, peer, self._recv_track(peer_id, peer, track))

        return peer

    async def _recv_track(self, peer_id, peer, track):
        first = True
        last_pts = None
        try:
            while self._current(peer_id, peer):
                frame = await track.recv()
                if not self._current(peer_id, peer):
                    break
                if frame.pts is None or (
                    last_pts is not None and frame.pts <= last_pts
                ):
                    raise ValueError("video presentation timestamp did not advance")
                last_pts = frame.pts
                pixels = frame.to_ndarray(format="bgr24")
                source = self._publishers.get(peer_id)
                if source is None:
                    raise PermissionError("video source identity is missing")
                if self._on_frame is not None:
                    self._on_frame(track.id, pixels, source)
                if first:
                    self._emit_status("receiving", "首帧已到达")
                    first = False
        except MediaStreamError:
            if self._current(peer_id, peer):
                self._schedule_cleanup(peer_id, peer)
        finally:
            track.stop()

    async def _on_offer(self, data):
        peer_id = data["from"]
        if peer_id not in self._publishers:
            raise PermissionError("offer source is not a vehicle in this room")
        self._emit_status("receiving_offer", "正在建立 WebRTC 连接")
        peer = await self._create_peer(peer_id)
        async with asyncio.timeout(15):
            await peer.pc.setRemoteDescription(
                RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
            )
            await peer.pc.setLocalDescription(await peer.pc.createAnswer())
            answer = peer.pc.localDescription
            await self.signaling.send(
                {
                    "type": "answer",
                    "to": peer_id,
                    "sdp": answer.sdp,
                    "sdpType": answer.type,
                }
            )

    def _peer_removed(self, peer_id):
        # Called after identity is removed; tasks retain their own peer references.
        source = self._publishers.get(peer_id)
        if source is not None and self._on_invalidate is not None:
            self._on_invalidate(source)
        live = set(self.peers.values())
        self._track_ids = {
            peer: ids for peer, ids in self._track_ids.items() if peer in live
        }

    async def _peer_failed(self, peer_id):
        # Rejoining gives this receiver a new identity and prompts the publisher
        # to offer a fresh ICE allocation. Other receivers keep their sessions.
        await self.signaling.close()

    async def _on_peer_left(self, data):
        await super()._on_peer_left(data)
        self._publishers.pop(data["id"], None)

    def _final_close(self):
        self._publishers.clear()
        self._track_ids.clear()

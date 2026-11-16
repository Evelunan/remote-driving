"""Vehicle video publisher with per-peer subscriptions and authenticated signaling."""

import asyncio

from rd_webrtc.session import MediaSession

from .track_manager import TrackManager


class WebRTCStreamer(MediaSession):
    def __init__(self, room, network, credentials, *, session, encoder_workers=None):
        if type(session) is not int or not 0 <= session <= 65535:
            raise ValueError("publisher control session must be uint16")
        super().__init__(room, network, credentials, publication_session=session)
        self.track_manager = TrackManager()
        self.encoding = {}
        self.encoding_pool = None
        if encoder_workers is not None:
            from rd_webrtc.encoding import EncodingPool

            self.encoding_pool = EncodingPool(encoder_workers)
        self.signaling.on("new_peer", self._on_new_peer)
        self.signaling.on("answer", self._on_answer)

    async def _create_peer(self, peer_id):
        peer = await super()._create_peer(peer_id)
        for track in self.track_manager.create_tracks(peer_id).values():
            peer.add_track(
                track,
                encoding_pool=self.encoding_pool,
                encoding=self.encoding.get(track.id),
            )
        return peer

    async def _close_owned(self):
        try:
            await super()._close_owned()
        finally:
            if self.encoding_pool is not None:
                await self.encoding_pool.close()

    def _peer_removed(self, peer_id):
        self.track_manager.close_tracks(peer_id)

    def _final_close(self):
        self.track_manager.close()

    async def _on_joined(self, data):
        roles = data.get("peer_roles", {})
        for peer_id in data["peers"]:
            if roles.get(peer_id) in {"driving", "viewer"}:
                await self._handle_new_peer(peer_id)

    async def _on_new_peer(self, data):
        if data.get("role") in {"driving", "viewer"}:
            await self._handle_new_peer(data["id"])

    async def _handle_new_peer(self, peer_id):
        peer = await self._create_peer(peer_id)
        async with asyncio.timeout(15):
            offer = await peer.create_offer()
            await self.signaling.send(
                {
                    "type": "offer",
                    "to": peer_id,
                    "sdp": offer.sdp,
                    "sdpType": offer.type,
                }
            )

    async def _on_answer(self, data):
        peer = self.peers.get(data["from"])
        if peer is not None:
            await peer.set_answer(data["sdp"], data["sdpType"])

    def register_video_track(self, name, fps=30, max_age_s=0.5, *, encoding=None):
        if self._closed:
            raise RuntimeError("streamer is closed")
        if encoding is not None and self.encoding_pool is None:
            raise ValueError("camera encoding requires encoder_workers")
        self.track_manager.register(name, fps, max_age_s)
        self.encoding[name] = encoding

    async def push_frame(self, image, track_name):
        return self.track_manager.publish(track_name, image)

    def invalidate_frame(self, track_name):
        self.track_manager.invalidate(track_name)

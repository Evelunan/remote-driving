"""Vehicle video publisher with per-peer subscriptions and authenticated signaling."""

from rd_core.async_compat import async_timeout
from rd_webrtc.session import MediaSession
from rd_webrtc.video_timing import PublisherTiming

from .track_manager import TrackManager


class WebRTCStreamer(MediaSession):
    def __init__(
        self,
        room,
        network,
        credentials,
        *,
        session,
        encoder_workers=None,
        on_viewers_changed=None,
    ):
        if type(session) is not int or not 0 <= session <= 65535:
            raise ValueError("publisher control session must be uint16")
        super().__init__(room, network, credentials, publication_session=session)
        self.track_manager = TrackManager()
        self.encoding = {}
        self.encoding_pool = None
        self._viewers = set()
        self._on_viewers_changed = on_viewers_changed
        if encoder_workers is not None:
            from rd_webrtc.encoding import EncodingPool

            self.encoding_pool = EncodingPool(encoder_workers)
        self.signaling.on("new_peer", self._on_new_peer)
        self.signaling.on("answer", self._on_answer)

    async def _create_peer(self, peer_id):
        peer = await super()._create_peer(peer_id)
        timing = PublisherTiming(peer.pc)
        for track in self.track_manager.create_tracks(peer_id).values():
            sender = peer.add_track(
                track,
                encoding_pool=self.encoding_pool,
                encoding=self.encoding.get(track.id),
            )
            timing.bind(sender, track)
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

    def _update_viewers(self, viewers):
        had_viewers = bool(self._viewers)
        self._viewers = viewers
        if self._on_viewers_changed is not None and had_viewers != bool(viewers):
            self._on_viewers_changed(bool(viewers))

    async def _disconnect(self):
        self._update_viewers(set())
        await super()._disconnect()

    async def _on_joined(self, data):
        roles = data.get("peer_roles", {})
        viewers = [
            peer_id
            for peer_id in data["peers"]
            if roles.get(peer_id) in {"driving", "viewer"}
        ]
        self._update_viewers(set(viewers))
        for peer_id in viewers:
            await self._handle_new_peer(peer_id)

    async def _on_new_peer(self, data):
        if data.get("role") in {"driving", "viewer"}:
            peer_id = data["id"]
            self._update_viewers(self._viewers | {peer_id})
            await self._handle_new_peer(peer_id)

    async def _on_peer_left(self, data):
        self._update_viewers(self._viewers - {data["id"]})
        await super()._on_peer_left(data)

    async def _handle_new_peer(self, peer_id):
        peer = await self._create_peer(peer_id)
        # ICE gathering runs outside the signaling reader so a departing
        # viewer can cancel negotiation before the offer timeout.
        self._spawn_peer_task(
            peer_id, peer, self._send_offer(peer_id, peer), name="media-offer"
        )

    async def _send_offer(self, peer_id, peer):
        async with async_timeout(15):
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

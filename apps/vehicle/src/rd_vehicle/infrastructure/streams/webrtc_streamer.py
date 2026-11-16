"""Vehicle media publisher: capture sources survive individual peer reconnects."""

import asyncio

from rd_core.logging import get_logger

from .peer_connection import PeerConnectionManager
from .signaling_client import SignalingClient
from .track_manager import TrackManager

logger = get_logger(__name__)


class WebRTCStreamer:
    def __init__(self, room, config):
        self.room, self.config = room, config
        self.signaling = SignalingClient(config.signaling.url)
        self.track_manager = TrackManager()
        self.peers = {}
        self.my_id = None
        self._task = None
        self._closed = False
        self._close_task = None
        self._peer_cleanup = set()
        self._closing_peers = set()
        for kind, handler in (
            ("joined", self._on_joined),
            ("new_peer", self._on_new_peer),
            ("answer", self._on_answer),
            ("candidate", self._on_candidate),
            ("peer_left", self._on_peer_left),
        ):
            self.signaling.on(kind, handler)

    async def start(self):
        if self._closed or self._task is not None:
            raise RuntimeError("streamer is single-use")
        self._task = asyncio.current_task()
        try:
            while not self._closed:
                try:
                    await self.signaling.connect()
                    await self.signaling.send({"type": "join", "room": self.room})
                    await self.signaling.listen()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - network reconnect boundary
                    logger.warning(
                        "media signaling disconnected (%s)", type(exc).__name__
                    )
                finally:
                    await self._close_connection()
                if not self._closed:
                    await asyncio.sleep(3)
        finally:
            self._closed = True
            self.track_manager.close()

    async def _close_connection(self):
        if self._peer_cleanup:
            await asyncio.gather(*tuple(self._peer_cleanup), return_exceptions=True)
        self.track_manager.close_tracks()
        peers = set(self.peers.values()) | self._closing_peers
        self.peers = {}
        results = await asyncio.gather(
            *(peer.close() for peer in peers),
            self.signaling.close(),
            return_exceptions=True,
        )
        self.my_id = None
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            self._closing_peers.update(peers)
            raise ExceptionGroup("media connection cleanup failed", errors)
        self._closing_peers.clear()

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()
            results = await asyncio.gather(self._task, return_exceptions=True)
            if isinstance(results[0], Exception):
                raise results[0]
        else:
            await self._close_connection()
        self.track_manager.close()

    async def _remove_peer(self, peer_id, expected=None):
        pc = self.peers.get(peer_id)
        if pc is None or (expected is not None and pc is not expected):
            return
        self.peers.pop(peer_id)
        self._closing_peers.add(pc)
        self.track_manager.close_tracks(peer_id)
        await pc.close()
        self._closing_peers.discard(pc)

    async def _create_peer(self, peer_id):
        await self._remove_peer(peer_id)
        pc = PeerConnectionManager(self.config.ice_servers)
        self.peers[peer_id] = pc
        for track in self.track_manager.create_tracks(peer_id).values():
            pc.add_track(track)

        @pc.pc.on("connectionstatechange")
        def on_state():
            if pc.pc.connectionState == "failed":
                task = asyncio.create_task(self._remove_peer(peer_id, pc))
                self._peer_cleanup.add(task)
                task.add_done_callback(self._peer_cleanup.discard)
                task.add_done_callback(self._observe_cleanup)

        return pc

    @staticmethod
    def _observe_cleanup(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "media peer cleanup failed (%s)", type(task.exception()).__name__
            )

    async def _on_joined(self, data):
        self.my_id = data["id"]
        for peer_id in data.get("peers", []):
            await self._handle_new_peer(peer_id)

    async def _on_new_peer(self, data):
        await self._handle_new_peer(data["id"])

    async def _handle_new_peer(self, peer_id):
        pc = await self._create_peer(peer_id)
        async with asyncio.timeout(15):
            offer = await pc.create_offer()
            await self.signaling.send(
                {
                    "type": "offer",
                    "to": peer_id,
                    "sdp": offer.sdp,
                    "sdpType": offer.type,
                }
            )

    async def _on_answer(self, data):
        pc = self.peers.get(data["from"])
        if pc is not None:
            await pc.set_answer(data["sdp"], data["sdpType"])

    async def _on_candidate(self, data):
        pc = self.peers.get(data["from"])
        if pc is not None:
            await pc.add_candidate(data["candidate"])

    async def _on_peer_left(self, data):
        await self._remove_peer(data["id"])

    def register_video_track(self, name, fps=30, max_age_s=0.5):
        if self._closed:
            raise RuntimeError("streamer is closed")
        self.track_manager.register(name, fps, max_age_s)

    async def push_frame(self, image, track_name):
        return self.track_manager.publish(track_name, image)

    def invalidate_frame(self, track_name):
        self.track_manager.invalidate(track_name)

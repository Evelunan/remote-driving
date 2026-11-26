"""Authenticated media receiver with owned, generation-scoped decode tasks."""

import time

from aiortc import RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from rd_core.async_compat import async_timeout
from rd_core.settings import VideoSafetySettings
from rd_core.video_safety import VideoSource
from rd_webrtc.session import MediaSession
from rd_webrtc.video_timing import ReceiverTiming


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


class _MediaOutage:
    """Recover an established peer if a full outage leaves its primary stream delayed."""

    def __init__(self, policy):
        self.policy = policy
        self.last_delivery = None
        self.resumed_at = None
        self.had_fresh_primary = False

    def needs_reconnect(self, view, captured_at, now):
        timeout = self.policy.stale_frame_ms / 1000
        if (
            self.had_fresh_primary
            and self.last_delivery is not None
            and now - self.last_delivery >= timeout
        ):
            self.resumed_at = now
        self.last_delivery = now
        if view != self.policy.primary_view or captured_at is None:
            return False
        age = now - captured_at
        if 0 <= age < self.policy.max_capture_age_ms / 1000:
            self.had_fresh_primary = True
            self.resumed_at = None
            return False
        return (
            self.resumed_at is not None
            and age >= self.policy.max_capture_age_ms / 1000
            and now - self.resumed_at >= max(timeout, self.policy.recovery_ms / 1000)
        )


class WebRTCReceiver(MediaSession):
    def __init__(
        self,
        room,
        network,
        credentials,
        *,
        views=("front", "back", "left", "right"),
        video_policy=None,
        low_latency_vp8=False,
    ):
        super().__init__(room, network, credentials)
        self._video_policy = video_policy or VideoSafetySettings()
        self._low_latency_vp8 = low_latency_vp8
        self._on_frame = None
        self._include_capture_time = False
        self._on_invalidate = None
        self._views = frozenset(views)
        self._publishers = {}
        self._track_ids = {}
        self.signaling.on("offer", self._on_offer)
        self.signaling.on("new_peer", self._on_new_peer)

    def on_frame(self, callback, *, include_capture_time=False):
        self._on_frame = callback
        self._include_capture_time = include_capture_time

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
        timing = ReceiverTiming()
        outage = _MediaOutage(self._video_policy)
        peer.pc.on("datachannel", timing.attach)

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
            if self._low_latency_vp8:
                from rd_webrtc.vp8_jitter import enable_low_latency_vp8

                transceiver = next(
                    t for t in peer.pc.getTransceivers() if t.receiver.track is track
                )
                enable_low_latency_vp8(transceiver)
            self._track_ids[peer].add(track.id)
            self._spawn_peer_task(
                peer_id, peer, self._recv_track(peer_id, peer, track, timing, outage)
            )

        return peer

    async def _recv_track(self, peer_id, peer, track, timing=None, outage=None):
        first = True
        last_pts = None
        receiver = (
            next((r for r in peer.pc.getReceivers() if r.track is track), None)
            if timing is not None
            else None
        )
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
                    if self._include_capture_time:
                        captured_at = (
                            timing.capture_time(receiver, frame.pts, conservative=True)
                            if receiver is not None
                            else None
                        )
                        if outage is not None and outage.needs_reconnect(
                            track.id, captured_at, time.monotonic()
                        ):
                            self._emit_status(
                                "connecting", "媒体中断后主画面持续滞后，正在重连"
                            )
                            self._schedule_cleanup(peer_id, peer)
                            return
                        self._on_frame(track.id, pixels, source, captured_at)
                    else:
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
        async with async_timeout(15):
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

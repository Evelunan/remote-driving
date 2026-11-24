import asyncio

from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp


class PeerConnectionManager:
    def __init__(self, ice_servers):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        self._on_ice_candidate = None
        self._sender_adapters = []
        self._close_task = None

    def on_ice_candidate(self, callback):
        # The aiortc peers exchange full SDP after setLocalDescription completes.
        # Keep this callback contract for peers that explicitly support trickle.
        self._on_ice_candidate = callback

    def add_track(self, track, *, encoding_pool=None, encoding=None):
        sender = self.pc.addTrack(track)
        if encoding_pool is not None:
            from .encoding import bind_sender

            self._sender_adapters.append(bind_sender(sender, encoding_pool, encoding))
            if encoding is not None:
                transceiver = next(
                    t for t in self.pc.getTransceivers() if t.sender is sender
                )
                transceiver.setCodecPreferences(
                    [
                        codec
                        for codec in RTCRtpSender.getCapabilities("video").codecs
                        if codec.mimeType.lower() in {"video/vp8", "video/rtx"}
                    ]
                )
        elif encoding is not None:
            raise ValueError("per-view encoding requires a shared encoding pool")
        return sender

    async def create_offer(self):
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        return self.pc.localDescription

    async def set_answer(self, sdp, type_):
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))

    async def add_candidate(self, candidate):
        if candidate is None or candidate.get("candidate") in (None, ""):
            await self.pc.addIceCandidate(None)
            return
        value = candidate["candidate"]
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError("invalid ICE candidate")
        mid, index = candidate.get("sdpMid"), candidate.get("sdpMLineIndex")
        if mid is None and index is None:
            raise ValueError("ICE candidate requires media identity")
        if index is not None and (type(index) is not int or index < 0):
            raise ValueError("invalid ICE media index")
        parsed = candidate_from_sdp(value.removeprefix("candidate:"))
        parsed.sdpMid, parsed.sdpMLineIndex = mid, index
        await self.pc.addIceCandidate(parsed)

    async def close(self):
        if self._close_task is None:
            # Cancelling aiortc's close can leave subsequent calls waiting
            # forever for its internal close future to complete.
            self._close_task = asyncio.create_task(
                self._close_owned(), name="media-peer-transport-close"
            )
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        try:
            await self.pc.close()
        finally:
            for unbind in self._sender_adapters:
                unbind()
            self._sender_adapters.clear()

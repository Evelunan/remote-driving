# peer_connection.py
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceCandidate,
)


class PeerConnectionManager:
    def __init__(self, ice_servers):
        self.pc = RTCPeerConnection(RTCConfiguration(ice_servers))
        self._on_ice_candidate = None

    def on_ice_candidate(self, callback):
        self._on_ice_candidate = callback

        @self.pc.on("icecandidate")
        async def handler(candidate):
            if candidate and self._on_ice_candidate:
                await self._on_ice_candidate(candidate)

    def add_track(self, track):
        self.pc.addTrack(track)

    async def create_offer(self):
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        return self.pc.localDescription

    async def set_answer(self, sdp, type_):
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))

    async def add_candidate(self, c):
        candidate = RTCIceCandidate(
            candidate=c["candidate"],
            sdpMid=c["sdpMid"],
            sdpMLineIndex=c["sdpMLineIndex"],
        )
        await self.pc.addIceCandidate(candidate)

    async def close(self):
        await self.pc.close()

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp


class PeerConnectionManager:
    def __init__(self, ice_servers):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        self._on_ice_candidate = None

    def on_ice_candidate(self, callback):
        # The aiortc peers exchange full SDP after setLocalDescription completes.
        # Keep this callback contract for peers that explicitly support trickle.
        self._on_ice_candidate = callback

    def add_track(self, track):
        self.pc.addTrack(track)

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
        await self.pc.close()

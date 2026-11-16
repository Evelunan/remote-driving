from unittest.mock import AsyncMock

import pytest
from rd_webrtc.peer import PeerConnectionManager


@pytest.mark.asyncio
async def test_browser_candidate_is_parsed_using_aiortc_sdp_api():
    peer = PeerConnectionManager([])
    try:
        peer.pc.addIceCandidate = AsyncMock()
        await peer.add_candidate(
            {
                "candidate": "candidate:1 1 udp 2122260223 127.0.0.1 5000 typ host",
                "sdpMid": "0",
                "sdpMLineIndex": 0,
            }
        )
        candidate = peer.pc.addIceCandidate.call_args.args[0]
        assert candidate.ip == "127.0.0.1" and candidate.port == 5000
        assert candidate.sdpMid == "0" and candidate.sdpMLineIndex == 0
        await peer.add_candidate(None)
        assert peer.pc.addIceCandidate.call_args.args == (None,)
        with pytest.raises(ValueError, match="media identity"):
            await peer.add_candidate(
                {"candidate": "candidate:1 1 udp 2122260223 127.0.0.1 5000 typ host"}
            )
    finally:
        await peer.close()

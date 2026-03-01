import asyncio
from typing import Optional, Callable, Any, Awaitable
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceServer,
    RTCIceCandidate,
)

from interfaces import IWebRTCClient
from config.loaders import WebRTCConfig


class AIORTCClient(IWebRTCClient):
    """基于 aiortc 的 WebRTC 客户端"""

    def __init__(self, ice_servers: Optional[list[RTCIceServer]] = None):
        self._ice_servers = ice_servers
        self._pc: Optional[RTCPeerConnection] = None
        self._on_track_callback: Optional[Callable[[Any], Awaitable[None]]] = None
        self._on_ice_candidate_callback: Optional[Callable[[dict], Awaitable[None]]] = (
            None
        )

    async def create_connection(self):
        # ✅ 防止重复创建导致泄漏
        if self._pc:
            await self.close_connection()

        self._pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=self._ice_servers)
        )

        # ===== track 监听 =====
        @self._pc.on("track")
        def on_track(track):
            if self._on_track_callback:
                asyncio.create_task(self._safe_track_callback(track))

        # ===== ICE candidate 监听 =====
        @self._pc.on("icecandidate")
        async def on_candidate(candidate):
            if candidate is None:
                return

            if self._on_ice_candidate_callback:
                try:
                    await self._on_ice_candidate_callback(
                        {
                            "candidate": candidate.candidate,
                            "sdpMid": candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        }
                    )
                except Exception:
                    # 避免异常污染 aiortc 内部循环
                    pass

        # ===== 连接状态监听（关键）=====
        @self._pc.on("connectionstatechange")
        async def on_connection_state_change():
            state = self._pc.connectionState
            if state in ("failed", "closed"):
                await self.close_connection()

    async def _safe_track_callback(self, track):
        try:
            await self._on_track_callback(track)
        except Exception:
            # 避免未捕获异常
            pass

    async def close_connection(self) -> None:
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
            finally:
                self._pc = None

    async def set_remote_description(self, sdp: str, sdp_type: str) -> None:
        if not self._pc:
            raise RuntimeError("PeerConnection not created")

        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type=sdp_type)
        )

    async def create_answer(self) -> tuple[str, str]:
        if not self._pc:
            raise RuntimeError("PeerConnection not created")

        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        return (
            self._pc.localDescription.sdp,
            self._pc.localDescription.type,
        )

    async def add_ice_candidate(self, candidate: dict) -> None:
        if not self._pc:
            return

        try:
            await self._pc.addIceCandidate(
                RTCIceCandidate(
                    sdpMid=candidate["sdpMid"],
                    sdpMLineIndex=candidate["sdpMLineIndex"],
                    candidate=candidate["candidate"],
                )
            )
        except Exception:
            # candidate 可能早于 remoteDescription 到达
            # 弱网环境常见，忽略即可
            pass

    def set_on_track_callback(self, callback: Callable[[Any], Awaitable[None]]) -> None:
        self._on_track_callback = callback

    def set_on_ice_candidate_callback(
        self, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._on_ice_candidate_callback = callback

    def is_connected(self) -> bool:
        return self._pc is not None

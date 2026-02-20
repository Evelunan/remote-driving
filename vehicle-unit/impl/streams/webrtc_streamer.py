# webrtc_streamer.py
import asyncio
import json
from typing import Any, Optional, Dict, Callable

import numpy as np
import websockets
from av import VideoFrame
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceCandidate,
    VideoStreamTrack,
)

from config.loaders import WebRTCConfig
from logger import get_logger
from interfaces import IStream

logger = get_logger(__name__)


# =========================================================
# Video Track
# =========================================================
class VideoFrameTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_event = asyncio.Event()
        self._closed = False
        # 会导致track关闭失败
        # self._lock = asyncio.Lock()

    async def update_frame(self, frame: np.ndarray):
        if self._closed:
            return

        # async with self._lock:
        self._latest_frame = frame.copy()
        self._frame_event.set()

    async def recv(self):
        if self._closed:
            raise asyncio.CancelledError

        pts, time_base = await self.next_timestamp()

        try:
            # 30fps tick，避免 1 秒卡顿
            await asyncio.wait_for(self._frame_event.wait(), timeout=1 / 30)
        except asyncio.TimeoutError:
            pass

        # async with self._lock:
        frame = self._latest_frame
        self._frame_event.clear()

        if frame is None:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)

        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    def stop(self):
        if not self._closed:
            self._closed = True
            self._frame_event.set()
        super().stop()


# =========================================================
# WebRTC Streamer
# =========================================================
class WebRTCStreamer(IStream):
    def __init__(self, room: str, config: WebRTCConfig):
        self.room = room
        self.config = config

        self.pc: Optional[RTCPeerConnection] = None
        self.ws: Optional[Any] = None
        self.track: Optional[VideoFrameTrack] = None

        self.my_id: Optional[str] = None
        self.peer_id: Optional[str] = None

        self._signaling_task: Optional[asyncio.Task] = None

        self._state_lock = asyncio.Lock()
        self._ws_send_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()

        self._closed = False
        self._reconnecting = False

        # 信令分发表
        self._handlers: Dict[str, Callable] = {
            "joined": self._on_joined,
            "new_peer": self._on_new_peer,
            "answer": self._on_answer,
            "candidate": self._on_candidate,
            "peer_left": self._on_peer_left,
        }

    # =====================================================
    # WebSocket
    # =====================================================
    async def _connect_ws(self):
        self.ws = await websockets.connect(self.config.signaling.url)
        await self._safe_ws_send({"type": "join", "room": self.room})

    async def _safe_ws_send(self, data: dict):
        if not self.ws:
            return
        async with self._ws_send_lock:
            await self.ws.send(json.dumps(data))

    # =====================================================
    # PeerConnection Lifecycle
    # =====================================================
    async def _close_pc(self):
        if self.pc:
            try:
                await self.pc.close()
            except Exception:
                pass
            self.pc = None

    async def _init_pc(self):
        self.pc = RTCPeerConnection(RTCConfiguration(self.config.ice_servers))

        if not self.track or self.track._closed:
            self.track = VideoFrameTrack()

        self.pc.addTrack(self.track)
        self._register_pc_handlers()

    async def _reset_pc(self):
        await self._close_pc()
        await self._init_pc()

    def _register_pc_handlers(self):
        @self.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate and self.my_id and self.peer_id:
                await self._safe_ws_send(
                    {
                        "type": "candidate",
                        "candidate": {
                            "candidate": candidate.candidate,
                            "sdpMid": candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        },
                        "from": self.my_id,
                        "to": self.peer_id,
                    }
                )

        @self.pc.on("iceconnectionstatechange")
        async def on_state_change():
            state = self.pc.iceConnectionState
            logger.info(f"ICE 状态: {state}")

            # if state in ("failed", "disconnected"):
            #     await self._handle_reconnect()

    # =====================================================
    # Offer / Answer
    # =====================================================
    async def _create_offer(self):
        if not self.pc or not self.peer_id:
            return

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        await self._safe_ws_send(
            {
                "type": "offer",
                "sdp": self.pc.localDescription.sdp,
                "sdpType": self.pc.localDescription.type,
                "from": self.my_id,
                "to": self.peer_id,
            }
        )

        logger.info("已发送 offer")

    # =====================================================
    # Reconnect
    # =====================================================
    async def _handle_reconnect(self):
        async with self._reconnect_lock:
            if self._closed or self._reconnecting:
                return

            self._reconnecting = True
            logger.info("执行重连...")

            try:
                await self._reset_pc()
                logger.info(f"PEER_ID: {self.peer_id}")
                if self.peer_id:
                    await self._create_offer()
            finally:
                self._reconnecting = False

    # =====================================================
    # Signaling Handlers
    # =====================================================
    async def _on_joined(self, data: dict):
        self.my_id = data.get("id")
        peers = data.get("peers", [])

        logger.info(f"已加入房间 {self.room}，ID: {self.my_id}，已有 peers: {peers}")

        if peers:
            self.peer_id = peers[0]
            await self._reset_pc()
            await self._create_offer()

    async def _on_new_peer(self, data: dict):
        peer_id = data.get("id")

        if not peer_id:
            return
        self.peer_id = peer_id

        await self._reset_pc()
        await self._create_offer()

    async def _on_answer(self, data: dict):
        if self.pc and self.pc.signalingState == "have-local-offer":
            await self.pc.setRemoteDescription(
                RTCSessionDescription(
                    sdp=data["sdp"],
                    type=data["sdpType"],
                )
            )
            logger.info("设置远端描述成功")

    async def _on_candidate(self, data: dict):
        if not self.pc:
            return

        c = data.get("candidate")
        if not c:
            return

        try:
            candidate = RTCIceCandidate(
                candidate=c["candidate"],
                sdpMid=c["sdpMid"],
                sdpMLineIndex=c["sdpMLineIndex"],
            )
            await self.pc.addIceCandidate(candidate)
        except Exception as e:
            logger.error(f"添加 ICE 失败: {e}")

    async def _on_peer_left(self, data: dict):
        left_id = data.get("id")
        if left_id == self.peer_id:
            logger.info(f"peer[{self.peer_id}]离开，等待重连...")
            self.peer_id = None

    # =====================================================
    # Signaling Loop
    # =====================================================
    async def _handle_incoming(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                handler = self._handlers.get(data.get("type"))
                if handler:
                    await handler(data)

        except websockets.ConnectionClosed:
            logger.info("信令连接关闭")
        except Exception as e:
            logger.exception(f"信令异常: {e}")

    # =====================================================
    # Public API
    # =====================================================
    async def start_stream(self):
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("流已关闭")

            await self._connect_ws()
            self._signaling_task = asyncio.create_task(self._handle_incoming())

    async def stream_frame(self, frame: np.ndarray):
        if self._closed or not self.track:
            return
        await self.track.update_frame(frame)

    async def close(self):
        async with self._state_lock:
            self._closed = True

            if self._signaling_task:
                self._signaling_task.cancel()
                try:
                    await self._signaling_task
                except asyncio.CancelledError:
                    pass
                self._signaling_task = None

            await self._close_pc()

            if self.track:
                self.track.stop()
                self.track = None

            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None

            logger.info("WebRTC 流已关闭")

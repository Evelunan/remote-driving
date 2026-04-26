# webrtc_streamer.py
import asyncio
from typing import Dict

from config.loaders import WebRTCConfig
from logger import get_logger

from .signaling_client import SignalingClient
from .peer_connection import PeerConnectionManager
from .track_manager import TrackManager

logger = get_logger(__name__)


class WebRTCStreamer:
    def __init__(self, room: str, config: WebRTCConfig):
        self.room = room
        self.config = config

        self.signaling = SignalingClient(config.signaling.url)
        self.track_manager = TrackManager()

        # 🔥 多 peer 支持
        self.peers: Dict[str, PeerConnectionManager] = {}

        self.my_id = None

    # =====================================================
    # 启动
    # =====================================================
    async def start(self):
        self._running = True
        
        while self._running:
            try:
                logger.info("正在连接信令服务器...")
                await self.signaling.connect()

                # 每次重连后清空旧状态
                for peer_id, pc in list(self.peers.items()):
                    await pc.close()
                self.peers.clear()

                # 注册信令回调
                self.signaling.on("joined", self._on_joined)
                self.signaling.on("new_peer", self._on_new_peer)
                self.signaling.on("answer", self._on_answer)
                self.signaling.on("candidate", self._on_candidate)
                self.signaling.on("peer_left", self._on_peer_left)

                await self.signaling.send({"type": "join", "room": self.room})

                # 阻塞直到断开
                await self.signaling.listen()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"信令服务器连接失败或断开: {e}")

            if self._running:
                logger.info("等待 3 秒后重连信令服务器...")
                await asyncio.sleep(3)

    async def close(self):
        self._running = False
        try:
            await self.signaling.close()
        except Exception as e:
            logger.warning(f"关闭 signaling 异常: {e}")

        for peer_id, pc in self.peers.items():
            try:
                await pc.close()
            except Exception as e:
                logger.warning(f"关闭 peer 失败: {e}")
        logger.info("推流端已关闭")

    # =====================================================
    # Peer 创建
    # =====================================================
    async def _create_peer(self, peer_id: str):
        pc = PeerConnectionManager(self.config.ice_servers)

        self.track_manager.reset_tracks()
        # 绑定 track
        for track in self.track_manager.all_tracks():
            pc.add_track(track)

        # ICE 回调
        async def on_ice(candidate):
            await self.signaling.send(
                {
                    "type": "candidate",
                    "from": self.my_id,
                    "to": peer_id,
                    "candidate": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    },
                }
            )

        pc.on_ice_candidate(on_ice)

        self.peers[peer_id] = pc
        return pc

    # =====================================================
    # 信令处理
    # =====================================================
    async def _on_joined(self, data):
        self.my_id = data["id"]
        existing_peers = data.get("peers", [])
        logger.info(f"peer joined: 房间-{self.room}, peers: {existing_peers}")
        for peer_id in existing_peers:
            await self._handle_new_peer(peer_id)

    async def _on_new_peer(self, data):
        peer_id = data["id"]
        logger.info(f"new peer: {peer_id}已加入")
        await self._handle_new_peer(peer_id)

    async def _handle_new_peer(self, peer_id):
        pc = await self._create_peer(peer_id)

        offer = await pc.create_offer()

        await self.signaling.send(
            {
                "type": "offer",
                "from": self.my_id,
                "to": peer_id,
                "sdp": offer.sdp,
                "sdpType": offer.type,
            }
        )

    async def _on_answer(self, data):
        peer_id = data["from"]
        pc = self.peers.get(peer_id)
        if pc:
            await pc.set_answer(data["sdp"], data["sdpType"])

    async def _on_candidate(self, data):
        peer_id = data["from"]
        pc = self.peers.get(peer_id)
        if pc:
            await pc.add_candidate(data["candidate"])

    async def _on_peer_left(self, data: dict):
        peer_id = data["id"]
        pc = self.peers.get(peer_id)
        if pc:
            await pc.close()
        self.peers.pop(peer_id, None)
        logger.info(f"{peer_id}已离开")

    # =====================================================
    # 对外接口
    # =====================================================
    def regist_video_track(self, name="default"):
        return self.track_manager.register(name)

    async def push_frame(self, frame, track_name="default"):
        track = self.track_manager.get_track(track_name)
        if track:
            await track.update_frame(frame)

    async def close(self):
        for pc in self.peers.values():
            await pc.close()
        await self.signaling.close()

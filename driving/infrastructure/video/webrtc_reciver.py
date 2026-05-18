# reciver.py
import asyncio
from typing import Callable, Optional, TYPE_CHECKING

from aiortc import RTCSessionDescription

from config.loaders import WebRTCConfig
from logger import get_logger

from .signaling_client_ import SignalingClient
from .peer_connection import PeerConnectionManager

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)


# 状态码常量
class VideoStatus:
    CONNECTING = "connecting"  # 正在连接信令服务器
    JOINED = "joined"  # 已加入房间
    WAITING_PEER = "waiting_peer"  # 等待车端加入
    PEER_JOINED = "peer_joined"  # 车端已加入，等待视频流
    RECEIVING_OFFER = "receiving_offer"  # 收到 offer，正在建立连接
    CONNECTED = "connected"  # WebRTC 连接成功
    RECEIVING = "receiving"  # 正在接收视频
    PEER_LEFT = "peer_left"  # 车端离开
    DISCONNECTED = "disconnected"  # 连接断开
    ERROR = "error"  # 发生错误


class WebRTCReceiver:
    """WebRTC 视频接收端"""

    def __init__(self, room: str, config: WebRTCConfig):
        self.room = room
        self.config = config

        self.signaling = SignalingClient(config.signaling.url)
        self._listen_task: Optional[asyncio.Task] = None
        self._handlers = {}

        # 多 peer 支持
        self.peers: dict[str, PeerConnectionManager] = {}

        self.my_id: Optional[str] = None

        # 视频帧回调 (track_id, frame)
        self._on_frame: Optional[Callable[[str, "np.ndarray"], None]] = None

        # 运行状态
        self._running = False

        # 状态回调
        self._status_callback: Optional[Callable[[str, str], None]] = None

    def set_status_callback(self, callback: Callable[[str, str], None]) -> None:
        """设置状态变化回调函数

        Args:
            callback: 回调函数，参数为 (status_code, message)
        """
        self._status_callback = callback

    def _emit_status(self, status: str, message: str) -> None:
        """发送状态变化通知"""
        if self._status_callback:
            self._status_callback(status, message)

    # =====================================================
    # 启动
    # =====================================================
    async def start(self):
        """启动接收端，连接信令服务器并加入房间（包含断线重连机制）"""
        self._running = True

        while self._running:
            try:
                self._emit_status(VideoStatus.CONNECTING, "正在连接信令服务器...")
                await self.signaling.connect()

                # 每次重连后清空旧状态
                for peer_id, pc in list(self.peers.items()):
                    await pc.close()
                self.peers.clear()

                # 注册信令回调
                self._register_handlers()

                # 加入房间
                await self.signaling.send({"type": "join", "room": self.room})

                # 启动信令监听（阻塞当前循环，直到断线或手动关闭）
                await self._listen_loop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"信令服务器连接失败或断开: {e}")
                self._emit_status(VideoStatus.ERROR, "信令服务器异常，3秒后重连...")

            if self._running:
                logger.info("等待 3 秒后重连信令服务器...")
                await asyncio.sleep(3)

    def on_frame(self, callback: Callable[[str, "np.ndarray"], None]):
        """注册视频帧回调函数

        Args:
            callback: 回调函数，参数为 (track_id, frame)
                - track_id: track 标识（对应发送端的摄像头名称）
                - frame: numpy.ndarray (BGR格式)
        """
        self._on_frame = callback

    # =====================================================
    # Peer 管理
    # =====================================================
    async def _create_peer(self, peer_id: str):
        """创建 PeerConnection"""
        pc = PeerConnectionManager(self.config.ice_servers)

        # 设置 track 接收回调
        @pc.pc.on("track")
        def on_track(track):
            self._emit_status(VideoStatus.RECEIVING, "正在接收视频流...")
            logger.info(f"收到来自 {peer_id} 的 track: {track.kind}, id={track.id}")
            if track.kind == "video":
                asyncio.create_task(self._recv_track(track))

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

    async def _recv_track(self, track):
        """接收并处理视频 track"""
        track_id = track.id
        logger.info(f"开始接收{track_id} 的视频流...")
        while self._running:
            try:
                frame = await track.recv()
                img = frame.to_ndarray(format="bgr24")

                if self._on_frame:
                    self._on_frame(track_id, img)
                    # logger.info("收到帧")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"接收{track_id} 视频帧出错: {e}")
                self._emit_status(VideoStatus.ERROR, f"视频播放出错: {e}")
                break
        logger.info(f"停止接收{track_id} 的视频流")

    # =====================================================
    # 信令处理
    # =====================================================
    def _register_handlers(self):
        self._handlers = {
            "joined": self._on_joined,
            "offer": self._on_offer,
            "candidate": self._on_candidate,
            "peer_left": self._on_peer_left,
            "new_peer": self._on_new_peer,
        }

    async def _on_joined(self, data: dict):
        """处理加入房间响应"""
        self.my_id = data["id"]
        existing_peers = data.get("peers", [])
        logger.info("已加入房间: %s, 已有 peers: %s", self.room, existing_peers)
        if existing_peers:
            logger.info(
                f"已加入房间，车端在线 (peers: {len(existing_peers)})，等待视频流..."
            )
            self._emit_status(
                VideoStatus.PEER_JOINED,
                f"已加入房间，车端在线 (peers: {len(existing_peers)})，等待视频流...",
            )
        else:
            logger.info("已加入房间，等待推流端...")
            self._emit_status(VideoStatus.WAITING_PEER, "已加入房间，等待车端连接...")

    async def _on_offer(self, data):
        """处理收到的 offer，创建 answer"""
        peer_id = data["from"]
        logger.info("收到连接请求，正在建立 WebRTC 连接...")
        self._emit_status(
            VideoStatus.RECEIVING_OFFER, "收到连接请求，正在建立 WebRTC 连接..."
        )
        pc = await self._create_peer(peer_id)

        # 设置远程描述
        await pc.pc.setRemoteDescription(
            RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
        )

        # 创建 answer
        answer = await pc.pc.createAnswer()
        await pc.pc.setLocalDescription(answer)

        # 发送 answer
        await self.signaling.send(
            {
                "type": "answer",
                "from": self.my_id,
                "to": peer_id,
                "sdp": pc.pc.localDescription.sdp,
                "sdpType": pc.pc.localDescription.type,
            }
        )

        logger.info(f"已向 {peer_id} 发送 answer")
        self._emit_status(VideoStatus.CONNECTED, "WebRTC 连接已建立，等待视频流...")

    async def _on_candidate(self, data: dict):
        """处理 ICE candidate"""
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
        self._emit_status(VideoStatus.PEER_LEFT, "车端已离开，等待重新连接...")

    async def _on_new_peer(self, data: dict):
        peer_id = data["id"]
        logger.info(f"{peer_id}已加入，等待offer")
        self._emit_status(VideoStatus.PEER_JOINED, "车端已连接，等待视频流...")

    # =====================================================
    # 生命周期
    # =====================================================
    async def close(self):
        """关闭接收端"""
        if not self._running:
            return
        self._running = False
        self._emit_status(VideoStatus.DISCONNECTED, "已断开连接")
        try:
            await self.signaling.close()
        except Exception as e:
            logger.warning(f"关闭 signaling 异常: {e}")

        # start 循环会在 _running=False 时自然结束，不需要手动等待/取消 task

        for peer_id, pc in self.peers.items():
            logger.info(f"关闭与 {peer_id} 的连接")
            try:
                await pc.close()
            except Exception as e:
                logger.warning(f"关闭 peer 失败: {e}")

        logger.info("接收端已关闭")

    async def _listen_loop(self):
        logger.info("信令监听循环启动")
        while self._running:
            try:
                data = await self.signaling.recv()
                msg_type = data.get("type")
                handler = self._handlers.get(msg_type)
                if handler:
                    await handler(data)
                else:
                    logger.warning(f"未知消息类型: {msg_type}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"信令异常: {e}")
                self._emit_status(VideoStatus.ERROR, f"信令异常: {e}")

import asyncio
from enum import Enum
from typing import Optional, Callable

from logger import get_logger
from interfaces.video import IVideoRenderer, ISignalingClient, IWebRTCClient

logger = get_logger(__name__)


# 状态码常量
class VideoStatus(str, Enum):
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


class VideoReceiver:

    def __init__(
        self,
        signaling: ISignalingClient,
        webrtc: IWebRTCClient,
        renderer: IVideoRenderer,
    ):
        self._signaling = signaling
        self._webrtc = webrtc
        self._renderer = renderer
        self._my_id: Optional[str] = None
        self._peer_id: Optional[str] = None
        self._running = False
        self._status_callback: Optional[Callable[[str, str], None]] = None
        self._handlers = {
            "joined": self._on_joined,
            "offer": self._on_offer,
            "candidate": self._on_candidate,
            "peer_left": self._on_peer_left,
            "new_peer": self._on_new_peer,
        }

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

    async def start(self) -> None:
        """启动接收器"""
        self._running = True

        # 设置 WebRTC 回调
        self._webrtc.set_on_track_callback(self._on_track)
        self._webrtc.set_on_ice_candidate_callback(self._on_ice_candidate)

        # 连接信令服务器
        self._emit_status(VideoStatus.CONNECTING, "正在连接信令服务器...")
        await self._signaling.connect()

        # TODO : _handle_signaling 此处会阻塞，是否需要处理？
        # 处理信令消息
        await self._handle_signaling()

    async def stop(self) -> None:
        """停止接收器"""
        self._running = False
        try:
            self._emit_status(VideoStatus.DISCONNECTED, "已断开连接")
        except Exception:
            pass  # 忽略状态发送失败

        try:
            await self._webrtc.close_connection()
        except Exception as e:
            logger.debug(f"关闭 WebRTC 连接时出错: {e}")

        try:
            await self._signaling.disconnect()
        except Exception as e:
            logger.debug(f"断开信令连接时出错: {e}")

        try:
            self._renderer.close()
        except Exception as e:
            logger.debug(f"关闭渲染器时出错: {e}")

    async def _on_track(self, track) -> None:
        """处理接收到的 track"""
        logger.info("收到轨道: %s", track.kind)
        self._emit_status(VideoStatus.RECEIVING, "正在接收视频流...")
        try:
            while self._running and self._renderer.is_active():
                frame = await track.recv()
                img = frame.to_ndarray(format="bgr24")
                if not self._renderer.render(img):
                    break
        except Exception as e:
            logger.error("播放出错: %s", e)
            self._emit_status(VideoStatus.ERROR, f"视频播放出错: {e}")
        finally:
            self._renderer.close()

    async def _on_ice_candidate(self, candidate: dict) -> None:
        """处理 ICE 候选"""
        if self._peer_id:
            await self._signaling.send(
                {
                    "type": "candidate",
                    "candidate": candidate,
                    "from": self._my_id,
                    "to": self._peer_id,
                }
            )

    async def _handle_signaling(self) -> None:
        """处理信令消息循环"""
        while self._running and self._signaling.is_connected():
            try:
                data = await self._signaling.receive()
                msg_type = data.get("type")
                handler = self._handlers.get(msg_type)

                if handler:
                    try:
                        await handler(data)
                    except Exception as e:
                        logger.error("处理信令消息失败 [%s]: %s", msg_type, e)
                        self._emit_status(VideoStatus.ERROR, f"处理消息失败: {e}")
                else:
                    logger.info("未知信令类型: %s", msg_type)

            except Exception as e:
                if self._running:
                    logger.error("信令错误: %s", e)
                    self._emit_status(VideoStatus.ERROR, f"信令错误: {e}")
                break

    async def _on_joined(self, data: dict) -> None:
        self._my_id = data["id"]
        peers = data.get("peers", [])
        logger.info("已加入房间: %s, 已有 peers: %s", self._my_id, peers)

        if peers:
            self._emit_status(
                VideoStatus.PEER_JOINED,
                f"已加入房间，车端在线 (peers: {len(peers)})，等待视频流...",
            )
        else:
            self._emit_status(VideoStatus.WAITING_PEER, "已加入房间，等待车端连接...")

    async def _on_offer(self, data: dict):
        """处理 offer"""

        self._peer_id = data["from"]
        self._emit_status(
            VideoStatus.RECEIVING_OFFER, "收到连接请求，正在建立 WebRTC 连接..."
        )

        # 关闭旧连接
        if self._webrtc.is_connected():
            await self._webrtc.close_connection()
            await asyncio.sleep(0.05)

        # 创建新连接
        await self._webrtc.create_connection()
        await self._webrtc.set_remote_description(data["sdp"], "offer")

        # 创建并发送 answer
        answer_sdp, answer_type = await self._webrtc.create_answer()
        await self._signaling.send(
            {
                "type": "answer",
                "sdp": answer_sdp,
                "sdpType": answer_type,
                "from": self._my_id,
                "to": self._peer_id,
            }
        )
        logger.info("已发送 answer")
        self._emit_status(VideoStatus.CONNECTED, "WebRTC 连接已建立，等待视频流...")

    async def _on_candidate(self, data: dict):
        if data.get("to") != self._my_id:
            return

        if not self._webrtc.is_connected():
            return
        try:
            await self._webrtc.add_ice_candidate(data["candidate"])
        except Exception as e:
            logger.error("添加 ICE 候选失败: %s", e)

    async def _on_peer_left(self, data: dict):
        if data.get("id") != self._peer_id:
            return

        logger.warning("对方离开")

        self._peer_id = None
        await self._webrtc.close_connection()

        self._emit_status(
            VideoStatus.PEER_LEFT,
            "车端已离开，等待重新连接...",
        )

    async def _on_new_peer(self, data: dict):
        logger.info("新 peer 加入，等待 offer")

        self._emit_status(
            VideoStatus.PEER_JOINED,
            "车端已连接，等待视频流...",
        )

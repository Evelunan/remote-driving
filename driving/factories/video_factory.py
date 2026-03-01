from typing import Optional, Type, List
from aiortc import RTCIceServer

from logger import get_logger
from infrastructure.video import (
    VideoReceiver,
    WebSocketSignalingClient,
    AIORTCClient,
    QueueBasedRenderer,
)
from interfaces import ISignalingClient, IVideoRenderer, IWebRTCClient

logger = get_logger(__name__)


class VideoFactory:
    """
    视频相关对象工厂

    优化点：
    1. 明确返回类型
    2. 支持依赖注入（便于测试）
    3. 避免强耦合具体实现
    """

    @staticmethod
    def create_signaling_client(
        server_url: str,
        room: str,
        signaling_cls: Type[ISignalingClient] = WebSocketSignalingClient,
    ) -> ISignalingClient:
        return signaling_cls(server_url, room)

    @staticmethod
    def create_webrtc_client(
        ice_servers: Optional[List[RTCIceServer]] = None,
        webrtc_cls: Type[IWebRTCClient] = AIORTCClient,
    ) -> IWebRTCClient:
        return webrtc_cls(ice_servers)

    @staticmethod
    def create_video_receiver(
        room: str,
        server_url: str,
        ice_servers: Optional[List[RTCIceServer]] = None,
        renderer: Optional[IVideoRenderer] = None,
        signaling_cls: Type[ISignalingClient] = WebSocketSignalingClient,
        webrtc_cls: Type[IWebRTCClient] = AIORTCClient,
    ) -> VideoReceiver:
        if renderer is None:
            renderer = QueueBasedRenderer()

        signaling = VideoFactory.create_signaling_client(
            server_url,
            room,
            signaling_cls,
        )

        webrtc = VideoFactory.create_webrtc_client(
            ice_servers,
            webrtc_cls,
        )

        return VideoReceiver(signaling, webrtc, renderer)

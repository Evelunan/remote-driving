from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Any
import numpy as np


class IVideoRenderer(ABC):
    """视频渲染器接口 - 负责显示视频帧"""

    @abstractmethod
    def render(self, frame: np.ndarray) -> bool:
        """
        渲染一帧视频

        Args:
            frame: BGR 格式的 numpy 数组

        Returns:
            bool: False 表示应该停止渲染（如用户关闭窗口）
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """关闭渲染器，释放资源"""
        pass

    @abstractmethod
    def is_active(self) -> bool:
        """检查渲染器是否仍然活跃"""
        pass


class ISignalingClient(ABC):
    """信令客户端接口 - 负责 WebSocket 信令通信"""

    @abstractmethod
    async def connect(self) -> None:
        """连接到信令服务器"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接"""
        pass

    @abstractmethod
    async def send(self, message: dict) -> None:
        """发送消息"""
        pass

    @abstractmethod
    async def receive(self) -> dict:
        """接收消息"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        pass


class IWebRTCClient(ABC):
    """WebRTC 客户端接口 - 负责 WebRTC 连接管理"""

    @abstractmethod
    async def create_connection(self) -> None:
        """创建 RTCPeerConnection"""
        pass

    @abstractmethod
    async def close_connection(self) -> None:
        """关闭连接"""
        pass

    @abstractmethod
    async def set_remote_description(self, sdp: str, sdp_type: str) -> None:
        """设置远端 SDP"""
        pass

    @abstractmethod
    async def create_answer(self) -> tuple[str, str]:
        """创建 Answer，返回 (sdp, type)"""
        pass

    @abstractmethod
    async def add_ice_candidate(self, candidate: dict) -> None:
        """添加 ICE 候选"""
        pass

    @abstractmethod
    def set_on_track_callback(self, callback: Callable[[Any], Awaitable[None]]) -> None:
        """设置收到 track 的回调"""
        pass

    @abstractmethod
    def set_on_ice_candidate_callback(
        self, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        """设置 ICE 候选回调"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        pass

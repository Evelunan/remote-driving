import threading
from typing import Optional
import numpy as np
from interfaces import IVideoRenderer


class QueueBasedRenderer(IVideoRenderer):
    """
    超低延迟渲染器

    设计原则：
    1. 零队列延迟：直接使用最新帧，不使用队列缓冲
    2. 无锁设计：使用原子引用交换，避免锁竞争
    3. 非阻塞：生产者和消费者都不会阻塞

    这是为远程驾驶等实时控制场景优化的渲染器，
    牺牲一定的帧完整性换取最低延迟。
    """

    def __init__(self):

        self._active = True
        self._dropped_frames = 0
        self._total_frames = 0

        # 超低延迟模式：只保留最新帧的引用
        # TODO:
        # 1. 确认是否需要 frame.copy() 防止上游复用 buffer 导致数据错乱
        # 2. 当前依赖 CPython GIL 保证引用赋值原子性，需明确仅支持 CPython
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_ready = threading.Event()
        self._frame_id = 0  # 帧计数器，用于检测新帧

    def render(self, frame: np.ndarray) -> bool:
        """
        接收新帧（由视频接收线程调用）

        超低延迟策略：直接覆盖旧帧，无队列缓冲
        """
        # TODO:
        # 1. 当前未统计真实丢帧次数
        #    若 _frame_ready 已 set，说明旧帧尚未被消费，应增加 _dropped_frames
        # 2. 若上游可能复用 frame，需要改为 frame.copy()
        if not self._active:
            return False

        self._total_frames += 1
        self._frame_id += 1
        self._latest_frame = frame
        self._frame_ready.set()  # 通知消费者有新帧

        return True

    # TODO:
    # 当前无法区分“新帧”和“旧帧”
    # 可考虑使用 frame_id 记录最后消费帧，避免重复显示

    def get_frame(self, timeout: float = 0.001) -> Optional[np.ndarray]:
        """
        获取最新帧（供 UI 线程调用）

        Args:
            timeout: 超时时间（秒），默认 1ms 确保最快响应

        Returns:
            帧数据，如果没有可用帧或渲染器已关闭则返回 None
        """
        if not self._active:
            return None

        # 等待新帧（极短超时）
        if self._frame_ready.wait(timeout=timeout):
            self._frame_ready.clear()
            return self._latest_frame
        return None

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """
        直接获取最新帧（跳过等待，最低延迟）

        注意：这可能导致帧重复显示
        """
        return self._latest_frame

    def drain_to_latest(self) -> Optional[np.ndarray]:
        """
        获取最新帧（兼容旧接口）

        在超低延迟模式下，直接返回最新帧
        """
        if not self._active:
            return None

        frame = self._latest_frame
        self._frame_ready.clear()
        return frame

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            "total_frames": self._total_frames,
            "dropped_frames": self._dropped_frames,
            "drop_rate": self._dropped_frames / max(1, self._total_frames),
            "frame_id": self._frame_id,
            "mode": "ultra_low_latency",
        }

    def close(self) -> None:
        self._active = False
        self._latest_frame = None
        self._frame_ready.set()  # 唤醒等待的消费者

    def is_active(self) -> bool:
        return self._active

    # TODO:
    # close() 和 request_stop() 逻辑重复
    # 可考虑合并为一个方法，避免维护两份相同代码

    def request_stop(self) -> None:
        """请求停止（供 UI 线程调用，例如当用户关闭窗口时）"""
        self._active = False
        self._latest_frame = None
        self._frame_ready.set()

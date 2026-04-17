import threading
import numpy as np
from logger import get_logger

logger = get_logger(__name__)


import threading
import numpy as np
from logger import get_logger

logger = get_logger(__name__)


class FrameBuffer:
    """固定4路摄像头缓存（线程安全版）"""

    def __init__(self):
        self.positions = ["front", "back", "left", "right"]
        self.frames = {p: None for p in self.positions}

        # ✅ 全局锁（简单可靠）
        self._lock = threading.Lock()

    def put(self, track_id: str, frame: np.ndarray):
        """存入帧（track_id 必须是固定四个之一）"""
        if track_id not in self.frames:
            raise ValueError(
                f"Invalid track_id: '{track_id}'. Must be one of {self.positions}"
            )

        # ✅ 写锁
        with self._lock:
            # ⚠️ 建议 copy，防止外部修改
            self.frames[track_id] = frame if frame is not None else None

    def get(self, position: str):
        """获取帧"""
        # ✅ 读锁
        with self._lock:
            frame = self.frames.get(position)

            # ⚠️ 防止外部修改内部数据
            return frame.copy() if frame is not None else None

    @property
    def front(self):
        return self.get("front")

    @property
    def back(self):
        return self.get("back")

    @property
    def left(self):
        return self.get("left")

    @property
    def right(self):
        return self.get("right")

    @property
    def all_frames(self):
        """一次性获取所有帧（保证一致性）"""
        with self._lock:
            return tuple(
                self.frames[p] if self.frames[p] is not None else None
                for p in self.positions
            )
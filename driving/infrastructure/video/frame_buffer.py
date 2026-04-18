from dataclasses import dataclass
import threading
import numpy as np
from logger import get_logger

logger = get_logger(__name__)


import threading
import numpy as np
from logger import get_logger

logger = get_logger(__name__)

@dataclass
class Frame:
    id: int = 0
    frame: np.ndarray = None

    def copy(self):
        return Frame(
            id=self.id, frame=self.frame.copy() if self.frame is not None else None
        )


class FrameBuffer:
    """固定4路摄像头缓存（线程安全版）"""

    def __init__(self):
        self.positions = ["front", "back", "left", "right"]
        self.frames = {p: Frame() for p in self.positions}

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
            self.frames[track_id].frame = frame
            self.frames[track_id].id += 1

    def get(self, position: str):
        """获取帧"""
        # ✅ 读锁
        with self._lock:
            frame = self.frames.get(position)

            return frame.copy() if frame is not None else None

    @property
    def all_frames(self):
        """一次性获取所有帧（保证一致性）"""
        with self._lock:
            return tuple(
                self.frames[p].copy() if self.frames[p] is not None else None
                for p in self.positions
            )

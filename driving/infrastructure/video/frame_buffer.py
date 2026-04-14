import threading
import numpy as np
from logger import get_logger

logger = get_logger(__name__)


class FrameBuffer:
    """固定4路摄像头缓存"""

    def __init__(self):
        self.positions = ["front", "back", "left", "right"]
        self.frames = {p: None for p in self.positions}
        # self._lock = threading.Lock()

    def put(self, track_id: str, frame: np.ndarray):
        """存入帧（track_id 必须是固定四个之一）"""
        if track_id not in self.frames:
            raise ValueError(
                f"Invalid track_id: '{track_id}'. Must be one of {self.positions}"
            )
        # logger.info(f"放入来自{track_id}的帧")
        self.frames[track_id] = frame

    def get(self, position: str):
        """获取帧"""
        return self.frames.get(position)

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
        return (
            self.frames["front"],
            self.frames["back"],
            self.frames["left"],
            self.frames["right"],
        )

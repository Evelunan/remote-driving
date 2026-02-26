import cv2
import numpy as np
from typing import List

from interfaces import IFrameCompositor


class DrivingCompositor(IFrameCompositor):
    """
    输入顺序:
        frames[0] -> 左摄像头
        frames[1] -> 前摄像头
        frames[2] -> 右摄像头
        frames[3] -> 后摄像头
    """

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height

        # 后置摄像头参数
        self.rear_height_ratio = 0.25
        self.rear_width_ratio = 0.25
        self.rear_position_y_ratio = 0.05

        # 预计算渐变背景（避免每帧循环）
        self._background = self._create_gradient_background()

    # ==========================
    # 背景生成（高效向量化）
    # ==========================
    def _create_gradient_background(self) -> np.ndarray:
        composite_width = self.width * 3
        gradient = np.linspace(80, 50, self.height).astype(np.uint8)
        bg = np.zeros((self.height, composite_width, 3), dtype=np.uint8)

        # BGR -> (0, G, G)
        bg[:, :, 1] = gradient[:, None]
        bg[:, :, 2] = gradient[:, None]

        return bg

    # ==========================
    # resize封装
    # ==========================
    def _resize(self, frame: np.ndarray, w: int, h: int) -> np.ndarray:
        return cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)

    # ==========================
    # 主合成函数
    # ==========================
    def compose_frames(self, frames: List[np.ndarray]) -> np.ndarray:
        if len(frames) < 4:
            raise ValueError("DrivingCompositor requires 4 frames: left, front, right, rear")

        composite = self._background.copy()

        # ================= LEFT =================
        frame_left = self._resize(frames[0], self.width, self.height)
        composite[:, 0:self.width] = frame_left

        # ================= FRONT =================
        frame_front = self._resize(frames[1], self.width, self.height)
        composite[:, self.width:self.width * 2] = frame_front

        # ================= RIGHT =================
        frame_right = self._resize(frames[2], self.width, self.height)
        composite[:, self.width * 2:self.width * 3] = frame_right

        # ================= REAR (Picture in Picture) =================
        rear_width = int(self.width * self.rear_width_ratio)
        rear_height = int(self.height * self.rear_height_ratio)

        frame_rear = self._resize(frames[3], rear_width, rear_height)

        rear_x = self.width + (self.width - rear_width) // 2
        rear_y = int(self.height * self.rear_position_y_ratio)

        # 边框
        border_thickness = 2
        cv2.rectangle(
            composite,
            (rear_x - border_thickness, rear_y - border_thickness),
            (rear_x + rear_width + border_thickness,
             rear_y + rear_height + border_thickness),
            (0, 0, 0),
            border_thickness
        )

        # 放置后摄像头
        composite[rear_y:rear_y + rear_height,
                  rear_x:rear_x + rear_width] = frame_rear

        # ================= 分隔线 =================
        line_color = (40, 40, 40)
        cv2.line(composite, (self.width, 0),
                 (self.width, self.height), line_color, 2)
        cv2.line(composite, (self.width * 2, 0),
                 (self.width * 2, self.height), line_color, 2)

        return composite

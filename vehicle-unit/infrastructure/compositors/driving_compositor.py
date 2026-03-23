import cv2
import numpy as np
from enum import Enum
from logger import get_logger

logger = get_logger(__name__)

class LayoutType(Enum):
    FRONT_DOMINANT = 1
    TRIPLE = 2
    GRID = 3


class DrivingCompositor:

    def __init__(self, width: int, height: int, layout=LayoutType.FRONT_DOMINANT):

        self.width = width
        self.height = height
        self.layout = layout

        # GPU 是否可用
        self.use_cuda = cv2.cuda.getCudaEnabledDeviceCount() > 0

        # 预分配画布
        self.canvas = np.zeros((height, width, 3), dtype=np.uint8)

        # UI参数
        self.line_color = (40, 40, 40)
        self.line_thickness = 2

        # FRONT 占比
        self.front_ratio = 0.7

    # =========================
    # 动态布局切换
    # =========================
    def set_layout(self, layout: LayoutType):
        if not self.use_cuda:
            logger.info("使用gpu")
        self.layout = layout

    # =========================
    # GPU resize
    # =========================
    def _resize(self, frame, w, h):
        if not self.use_cuda:
            return cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)

        gpu_frame = cv2.cuda_GpuMat()
        gpu_frame.upload(frame)

        gpu_resized = cv2.cuda.resize(
            gpu_frame, (w, h), interpolation=cv2.INTER_AREA
        )
        return gpu_resized.download()

    # =========================
    # FRONT DOMINANT
    # =========================
    def _compose_front_dominant(self, frames):
        left, front, right, rear = frames
        W = self.width
        H = self.height

        front_h = int(H * self.front_ratio)
        bottom_h = H - front_h
        cell_w = W // 3
        last_w = W - cell_w * 2  # 处理整数除法余数

        canvas = self.canvas
        canvas[:] = 0

        # FRONT
        f = self._resize(front, W, front_h)
        canvas[0:front_h, 0:W] = f

        # LEFT
        l = self._resize(left, cell_w, bottom_h)
        canvas[front_h:H, 0:cell_w] = l

        # REAR
        r = self._resize(rear, cell_w, bottom_h)
        canvas[front_h:H, cell_w:cell_w * 2] = r

        # RIGHT (使用剩余宽度)
        rr = self._resize(right, last_w, bottom_h)
        canvas[front_h:H, cell_w * 2:W] = rr

        # 分隔线
        cv2.line(canvas, (0, front_h), (W, front_h),
                 self.line_color, self.line_thickness)

        cv2.line(canvas, (cell_w, front_h), (cell_w, H),
                 self.line_color, self.line_thickness)

        cv2.line(canvas, (cell_w * 2, front_h), (cell_w * 2, H),
                 self.line_color, self.line_thickness)

        return canvas

    # =========================
    # TRIPLE 布局
    # =========================
    def _compose_triple(self, frames):
        left, front, right, rear = frames

        W = self.width
        H = self.height

        cell_w = W // 3
        last_w = W - cell_w * 2  # 处理整数除法余数

        canvas = self.canvas
        canvas[:] = 0

        l = self._resize(left, cell_w, H)
        f = self._resize(front, cell_w, H)
        r = self._resize(right, last_w, H)

        canvas[:, 0:cell_w] = l
        canvas[:, cell_w:cell_w * 2] = f
        canvas[:, cell_w * 2:W] = r

        # rear pip
        pip_w = int(cell_w * 0.6)
        pip_h = int(H * 0.3)

        rear_frame = self._resize(rear, pip_w, pip_h)

        x = cell_w + (cell_w - pip_w) // 2
        y = int(H * 0.05)

        canvas[y:y + pip_h, x:x + pip_w] = rear_frame

        return canvas

    # =========================
    # GRID 布局
    # =========================
    def _compose_grid(self, frames):

        left, front, right, rear = frames

        W = self.width
        H = self.height

        half_w = W // 2
        half_h = H // 2
        right_w = W - half_w  # 处理整数除法余数
        bottom_h = H - half_h  # 处理整数除法余数

        canvas = self.canvas
        canvas[:] = 0

        canvas[0:half_h, 0:half_w] = self._resize(left, half_w, half_h)
        canvas[0:half_h, half_w:W] = self._resize(front, right_w, half_h)
        canvas[half_h:H, 0:half_w] = self._resize(right, half_w, bottom_h)
        canvas[half_h:H, half_w:W] = self._resize(rear, right_w, bottom_h)

        return canvas

    # =========================
    # 主函数
    # =========================
    def compose_frames(self, frames: list[np.ndarray]) -> np.ndarray:
        if len(frames) < 4:
            # 使用黑帧填充缺失的帧
            black_frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            while len(frames) < 4:
                frames.append(black_frame.copy())

        if self.layout == LayoutType.FRONT_DOMINANT:
            return self._compose_front_dominant(frames)

        if self.layout == LayoutType.TRIPLE:
            return self._compose_triple(frames)

        if self.layout == LayoutType.GRID:
            return self._compose_grid(frames)

        return self._compose_front_dominant(frames)
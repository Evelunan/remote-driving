from typing import Optional

from .frame_buffer import FrameBuffer
import cv2
import numpy as np


class MultiCameraDisplay:
    """四路摄像头显示渲染器"""

    def __init__(self, width: int = 1280, height: int = 720):
        self.width = width
        self.height = height
        self.line_color = (255, 255, 255)
        self.line_thickness = 2
        self.front_ratio = 0.6

    def compose(self, buffer: FrameBuffer, layout: str = "front_dominant") -> np.ndarray:
        """合成显示画面"""
        if layout == "front_dominant":
            return self._compose_front_dominant(buffer)
        elif layout == "triple":
            return self._compose_triple(buffer)
        else:
            return self._compose_grid(buffer)

    def _resize(self, frame: Optional[np.ndarray], w: int, h: int) -> np.ndarray:
        """等比缩放，保持原始宽高比，黑边填充"""
        canvas = np.zeros((h, w, 3), dtype=np.uint8)
        if frame is None:
            return canvas

        fh, fw = frame.shape[:2]
        scale = min(w / fw, h / fh)
        new_w, new_h = int(fw * scale), int(fh * scale)

        resized = cv2.resize(frame, (new_w, new_h))

        x = (w - new_w) // 2
        y = (h - new_h) // 2
        canvas[y : y + new_h, x : x + new_w] = resized
        return canvas

    def _compose_grid(self, buffer: FrameBuffer) -> np.ndarray:
        W, H = self.width, self.height
        half_w, half_h = W // 2, H // 2
        right_w, bottom_h = W - half_w, H - half_h

        canvas = np.zeros((H, W, 3), dtype=np.uint8)

        canvas[0:half_h, 0:half_w] = self._resize(buffer.get("front"), half_w, half_h)
        canvas[0:half_h, half_w:W] = self._resize(buffer.get("back"), right_w, half_h)
        canvas[half_h:H, 0:half_w] = self._resize(buffer.get("left"), half_w, bottom_h)
        canvas[half_h:H, half_w:W] = self._resize(
            buffer.get("right"), right_w, bottom_h
        )

        cv2.line(canvas, (half_w, 0), (half_w, H), self.line_color, self.line_thickness)
        cv2.line(canvas, (0, half_h), (W, half_h), self.line_color, self.line_thickness)

        self._draw_label(canvas, "FRONT", 10, 30)
        self._draw_label(canvas, "BACK", half_w + 10, 30)
        self._draw_label(canvas, "LEFT", 10, half_h + 30)
        self._draw_label(canvas, "RIGHT", half_w + 10, half_h + 30)

        return canvas

    def _compose_front_dominant(self, buffer: FrameBuffer) -> np.ndarray:
        W, H = self.width, self.height
        front_h = int(H * self.front_ratio)
        bottom_h = H - front_h
        cell_w = W // 3
        last_w = W - cell_w * 2

        canvas = np.zeros((H, W, 3), dtype=np.uint8)

        canvas[0:front_h, 0:W] = self._resize(buffer.get("front"), W, front_h)
        canvas[front_h:H, 0:cell_w] = self._resize(buffer.get("left"), cell_w, bottom_h)
        canvas[front_h:H, cell_w : cell_w * 2] = self._resize(
            buffer.get("back"), cell_w, bottom_h
        )
        canvas[front_h:H, cell_w * 2 : W] = self._resize(
            buffer.get("right"), last_w, bottom_h
        )

        cv2.line(
            canvas, (0, front_h), (W, front_h), self.line_color, self.line_thickness
        )
        cv2.line(
            canvas, (cell_w, front_h), (cell_w, H), self.line_color, self.line_thickness
        )
        cv2.line(
            canvas,
            (cell_w * 2, front_h),
            (cell_w * 2, H),
            self.line_color,
            self.line_thickness,
        )

        self._draw_label(canvas, "FRONT", 10, 30)
        self._draw_label(canvas, "LEFT", 10, front_h + 30)
        self._draw_label(canvas, "BACK", cell_w + 10, front_h + 30)
        self._draw_label(canvas, "RIGHT", cell_w * 2 + 10, front_h + 30)

        return canvas

    def _compose_triple(self, buffer: FrameBuffer) -> np.ndarray:
        W, H = self.width, self.height
        cell_w = W // 3
        last_w = W - cell_w * 2

        canvas = np.zeros((H, W, 3), dtype=np.uint8)

        canvas[:, 0:cell_w] = self._resize(buffer.get("left"), cell_w, H)
        canvas[:, cell_w : cell_w * 2] = self._resize(buffer.get("front"), cell_w, H)
        canvas[:, cell_w * 2 : W] = self._resize(buffer.get("right"), last_w, H)

        pip_w, pip_h = int(cell_w * 0.6), int(H * 0.3)
        x, y = cell_w + (cell_w - pip_w) // 2, int(H * 0.05)

        canvas[y : y + pip_h, x : x + pip_w] = self._resize(
            buffer.get("back"), pip_w, pip_h
        )
        cv2.rectangle(
            canvas, (x, y), (x + pip_w, y + pip_h), self.line_color, self.line_thickness
        )

        cv2.line(canvas, (cell_w, 0), (cell_w, H), self.line_color, self.line_thickness)
        cv2.line(
            canvas,
            (cell_w * 2, 0),
            (cell_w * 2, H),
            self.line_color,
            self.line_thickness,
        )

        self._draw_label(canvas, "LEFT", 10, 30)
        self._draw_label(canvas, "FRONT", cell_w + 10, 30)
        self._draw_label(canvas, "RIGHT", cell_w * 2 + 10, 30)
        self._draw_label(canvas, "BACK", x + 5, y + 25)

        return canvas

    def _draw_label(self, canvas: np.ndarray, text: str, x: int, y: int):
        cv2.putText(canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

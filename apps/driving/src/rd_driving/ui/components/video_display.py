"""Paint the latest BGR frame when Qt presents this view."""

import time

import numpy as np
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget
from rd_driving.interfaces.ui import ITheme


class VideoDisplay(QWidget):
    def __init__(
        self,
        scaling_mode: str = "ultra_low_latency",
        parent: QWidget | None = None,
        *,
        debug_overlay=False,
        view_label: str = "",
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._view_label = view_label
        self.setAccessibleName(view_label)
        self._scaling_mode = scaling_mode
        self._debug_overlay = debug_overlay
        self._display_time = ""
        self._message = ""
        self._background = QColor("#0D1117")
        self._foreground = QColor("#6E7681")
        font = self.font()
        font.setPixelSize(18)
        self.setFont(font)

        self._current_frame: np.ndarray | None = None
        self._image = QImage()
        self._resolution = (0, 0)
        self._pending_capture = self._painted_capture = None
        self._last_frame_id = self._presented_id = -1
        self._on_presented = None
        self._fps = self._frame_count = 0
        self._last_fps_time = time.monotonic()

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: ITheme) -> None:
        self._background = QColor(theme.bg_primary)
        self._foreground = QColor(theme.text_muted)
        font = self.font()
        family = theme.get_font_family()
        if family:
            font.setFamily(family)
        self.setFont(font)
        self.update()

    def update_frame(
        self, frame: np.ndarray, frame_id: int, *, on_presented=None, captured_at=None
    ) -> None:
        if frame is None or frame_id == self._last_frame_id:
            return
        # Cached frames already own immutable, contiguous pixels. Take a snapshot
        # for callers that reuse a writable buffer before the queued paint.
        if frame.flags.writeable or not frame.flags.c_contiguous:
            frame = frame.copy(order="C")
        height, width = frame.shape[:2]
        image = QImage(
            frame.data, width, height, frame.strides[0], QImage.Format.Format_BGR888
        )
        if image.isNull():
            return
        self._image = image
        self._current_frame = frame
        self._resolution = (width, height)
        if self._message:
            self._message = ""
            self.setAccessibleDescription("")
        self._last_frame_id = frame_id
        self._pending_capture = captured_at
        self._on_presented = on_presented
        if self._debug_overlay:
            self._display_time = time.strftime("%H:%M:%S")
        self.update()

    def _count_painted_frame(self):
        self._frame_count += 1
        now = time.monotonic()
        elapsed = now - self._last_fps_time
        if elapsed >= 1.0:
            self._fps = int(self._frame_count / elapsed)
            self._frame_count = 0
            self._last_fps_time = now

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        if self._image.isNull():
            painter.setPen(self._foreground)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
        else:
            target_size = self._image.size().scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRect(0, 0, target_size.width(), target_size.height())
            target.moveCenter(self.rect().center())
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform,
                self._scaling_mode != "ultra_low_latency",
            )
            painter.drawImage(target, self._image)
            if self._debug_overlay:
                painter.setPen(QColor(0, 255, 0))
                painter.drawText(
                    12, self.height() - 12, "display: " + self._display_time
                )
        if self._view_label:
            font = painter.font()
            font.setPixelSize(14)
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            badge = QRect(
                8,
                8,
                metrics.horizontalAdvance(self._view_label) + 14,
                metrics.height() + 6,
            )
            painter.fillRect(badge, QColor("#17212B"))
            painter.setPen(QColor("#FFD18B") if self._message else QColor("#FFFFFF"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._view_label)
        painter.end()
        if not self._image.isNull() and self._presented_id != self._last_frame_id:
            self._count_painted_frame()
            self._presented_id = self._last_frame_id
            self._painted_capture = self._pending_capture
            if self._on_presented is not None:
                self._on_presented()

    def show_message(self, text: str) -> None:
        self._image = QImage()
        self._current_frame = None
        self._message = text
        self.setAccessibleDescription(text)
        self._resolution = (0, 0)
        self._pending_capture = self._painted_capture = None
        self._last_frame_id = self._presented_id = -1
        self._on_presented = None
        self._fps = self._frame_count = 0
        self._last_fps_time = time.monotonic()
        self.update()

    def set_scaling_mode(self, mode: str) -> None:
        if mode in ("quality", "balanced", "performance", "ultra_low_latency"):
            self._scaling_mode = mode
            self.update()

    def get_fps(self) -> int:
        return self._fps

    def get_latency_ms(self) -> int | None:
        if self._painted_capture is None:
            return None
        return round((time.monotonic() - self._painted_capture) * 1000)

    def get_resolution(self) -> tuple[int, int]:
        return self._resolution

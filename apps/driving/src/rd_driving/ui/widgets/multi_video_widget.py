"""
多路视频布局组件

支持两种布局模式：
- front_dominant: 前视主导，前视占上半部分，下方左/后/右三等分
- grid: 2×2 等分网格
"""


from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from rd_core.logging import get_logger
from rd_driving.infrastructure.video import FrameBuffer

from ..components import VideoDisplay

logger = get_logger(__name__)

# 布局模式常量
LAYOUT_FRONT_DOMINANT = "front_dominant"
LAYOUT_GRID = "grid"

CAMERA_NAMES = ("front", "back", "left", "right")
DEFAULT_LABELS = {
    "front": "前视",
    "back": "后视",
    "left": "左视",
    "right": "右视",
}

BORDER_COLOR = "#30363D"
BORDER_WIDTH = 1

_CELL_STYLE = f"""
    border: {BORDER_WIDTH}px solid {BORDER_COLOR};
    border-radius: 4px;
"""

_CONTAINER_STYLE = f"""
    border: {BORDER_WIDTH}px solid {BORDER_COLOR};
    border-radius: 6px;
"""


class _FrameWakeup(QObject):
    ready = pyqtSignal()

    def notify(self):
        self.ready.emit()


class MultiVideoWidget(QWidget):
    def __init__(
        self,
        fb: FrameBuffer,
        parent: QWidget | None = None,
        *,
        stale_frame_ms=500,
        debug_overlay=False,
        video_gate=None,
        primary_view="front",
    ):
        super().__init__(parent)

        self._fb = fb
        # A pending producer callback keeps the signal owner alive during teardown.
        self._wakeup = _FrameWakeup()
        self._wakeup.ready.connect(
            self._on_frames_ready, Qt.ConnectionType.QueuedConnection
        )
        self._video_gate, self._primary_view = video_gate, primary_view
        if stale_frame_ms <= 0:
            raise ValueError("stale_frame_ms must be positive")
        self._stale_after_s = stale_frame_ms / 1000
        self._availability = {name: "waiting" for name in CAMERA_NAMES}
        self._layout_mode: str | None = None

        # 四路 VideoDisplay
        self._displays: dict[str, VideoDisplay] = {}
        for cam in CAMERA_NAMES:
            d = VideoDisplay(
                scaling_mode="ultra_low_latency",
                debug_overlay=debug_overlay,
                view_label=DEFAULT_LABELS[cam],
            )
            d.show_message("等待视频流...")
            self._displays[cam] = d

        container = QFrame(self)
        container.setStyleSheet(_CONTAINER_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)
        self._grid = QGridLayout(container)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(2)
        for column in range(6):
            self._grid.setColumnStretch(column, 1)
        self._cells = {
            cam: self._wrap_in_frame(display) for cam, display in self._displays.items()
        }
        self._apply_layout(LAYOUT_FRONT_DOMINANT)

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _wrap_in_frame(display: VideoDisplay) -> QFrame:
        """将 VideoDisplay 包裹在带边框的 QFrame 中"""
        frame = QFrame()
        frame.setStyleSheet(_CELL_STYLE)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(display)
        return frame

    # ------------------------------------------------------------------ #
    #  布局切换
    # ------------------------------------------------------------------ #

    def _apply_layout(self, mode: str) -> None:
        if mode == LAYOUT_GRID:
            positions = (
                ("front", 0, 0, 3),
                ("back", 0, 3, 3),
                ("left", 1, 0, 3),
                ("right", 1, 3, 3),
            )
            row_weights = (1, 1)
        else:
            mode = LAYOUT_FRONT_DOMINANT
            positions = (
                ("front", 0, 0, 6),
                ("left", 1, 0, 2),
                ("back", 1, 2, 2),
                ("right", 1, 4, 2),
            )
            row_weights = (3, 2)
        for row, weight in enumerate(row_weights):
            self._grid.setRowStretch(row, weight)
        for name, row, column, span in positions:
            cell = self._cells[name]
            self._grid.removeWidget(cell)
            self._grid.addWidget(cell, row, column, 1, span)
        self._layout_mode = mode

    def set_layout(self, mode: str) -> None:
        """切换布局模式"""
        if mode == self._layout_mode:
            return
        self._apply_layout(mode)

    # ------------------------------------------------------------------ #
    #  帧刷新
    # ------------------------------------------------------------------ #

    @pyqtSlot()
    def _on_frames_ready(self):
        if self._fb.consume_notification(self._wakeup.notify):
            self.update()

    def check_expiry(self):
        self._refresh_frames(expiry_only=True)

    def update(self) -> None:
        self._refresh_frames(expiry_only=False)

    def _refresh_frames(self, *, expiry_only):
        for cam in CAMERA_NAMES:
            frame = self._fb.get(cam)
            state = (
                "waiting"
                if frame is None or frame.frame is None
                else "fresh"
                if frame.fresh(self._stale_after_s)
                else "stale"
            )
            if state == "fresh":
                if expiry_only:
                    continue
                callback = None
                if cam == self._primary_view and self._video_gate is not None:
                    callback = lambda f=frame: self._video_gate.observe(
                        f.id, f.received_at, f.source, captured_at=f.captured_at
                    )
                self._displays[cam].update_frame(
                    frame.frame,
                    frame.id,
                    on_presented=callback,
                    captured_at=frame.captured_at,
                )
            elif self._availability[cam] != state:
                if cam == self._primary_view and self._video_gate is not None:
                    self._video_gate.invalidate("picture_unavailable")
                message = "接收画面已过期" if state == "stale" else "等待视频流..."
                self._displays[cam].show_message(message)
            self._availability[cam] = state

    def apply_theme(self, theme) -> None:
        """为所有 VideoDisplay 应用主题"""
        for d in self._displays.values():
            d.apply_theme(theme)

    def show_message_all(self, message: str) -> None:
        if self._video_gate is not None:
            self._video_gate.invalidate("picture_unavailable")
        for display in self._displays.values():
            display.show_message(message)

    def showEvent(self, event):
        super().showEvent(event)
        self._fb.set_notifier(self._wakeup.notify)
        self.update()

    def hideEvent(self, event):
        self._fb.remove_notifier(self._wakeup.notify)
        if self._video_gate is not None:
            self._video_gate.invalidate("picture_hidden")
        super().hideEvent(event)

    def get_display(self, key=None):
        if key in CAMERA_NAMES:
            return self._displays[key]
        return None

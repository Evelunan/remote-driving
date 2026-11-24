"""Arrange four camera feeds while keeping the configured primary view visible."""

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from rd_driving.infrastructure.video import FrameBuffer
from rd_driving.interfaces.ui import ITheme

from ..components import VideoDisplay

# Keep the programmatic layout name for existing callers.
LAYOUT_FRONT_DOMINANT = "front_dominant"
LAYOUT_GRID = "grid"

CAMERA_NAMES = ("front", "back", "left", "right")
AUXILIARY_ORDER = ("left", "back", "right", "front")
DEFAULT_LABELS = {
    "front": "前视",
    "back": "后视",
    "left": "左视",
    "right": "右视",
}
_EMPTY_CAMERA_METRICS = "-- FPS   延迟 -- ms"


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
        if primary_view not in CAMERA_NAMES:
            raise ValueError("primary_view must be a supported camera")
        if stale_frame_ms <= 0:
            raise ValueError("stale_frame_ms must be positive")
        self._stale_after_s = stale_frame_ms / 1000
        self._availability = {name: "waiting" for name in CAMERA_NAMES}
        self._layout_mode: str | None = None

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        for column in range(6):
            self._grid.setColumnStretch(column, 1)

        self._displays: dict[str, VideoDisplay] = {}
        self._cells: dict[str, QFrame] = {}
        self._metrics: dict[str, QLabel] = {}
        for cam in CAMERA_NAMES:
            is_primary = cam == primary_view
            display = VideoDisplay(
                debug_overlay=debug_overlay,
                view_label=DEFAULT_LABELS[cam],
            )
            if not is_primary:
                display.setMinimumSize(160, 90)
            display.show_message("等待视频流...")
            metrics = QLabel(_EMPTY_CAMERA_METRICS, objectName="cameraMetrics")
            metrics.setToolTip(
                "帧率按实际绘制统计。延迟估算当前显示帧距车辆端读取该帧的时间。"
            )
            self._displays[cam] = display
            self._metrics[cam] = metrics
            self._cells[cam] = self._wrap_in_frame(display, metrics, is_primary)
        self._apply_layout(LAYOUT_FRONT_DOMINANT)

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _wrap_in_frame(
        display: VideoDisplay, metrics: QLabel, is_primary: bool
    ) -> QFrame:
        frame = QFrame()
        frame.setObjectName("cameraTile")
        frame.setProperty("primaryView", is_primary)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(10, 4, 10, 4)
        header.setSpacing(8)
        title = QLabel(display.accessibleName())
        title.setObjectName("cameraTitle")
        header.addWidget(title)
        if is_primary:
            marker = QLabel("主画面")
            marker.setObjectName("primaryCameraMarker")
            header.addWidget(marker)
        header.addStretch()
        header.addWidget(metrics)
        layout.addLayout(header)
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
            auxiliary_views = (
                cam for cam in AUXILIARY_ORDER if cam != self._primary_view
            )
            positions = ((self._primary_view, 0, 0, 6),) + tuple(
                (cam, 1, column * 2, 2) for column, cam in enumerate(auxiliary_views)
            )
            row_weights = (3, 1)
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
                self.set_camera_stats(cam, 0, None)
            self._availability[cam] = state

    def set_camera_stats(self, key: str, fps: int, latency_ms: int | None) -> None:
        latency = "--" if latency_ms is None else str(latency_ms)
        text = f"{fps or '--'} FPS   延迟 {latency} ms"
        label = self._metrics[key]
        if label.text() != text:
            label.setText(text)

    def apply_theme(self, theme: ITheme) -> None:
        self.setStyleSheet(
            f"""
            QFrame#cameraTile {{
                background-color: {theme.bg_card};
                border: 1px solid {theme.border};
            }}
            QFrame#cameraTile[primaryView="true"] {{
                border-color: {theme.border_highlight};
            }}
            QLabel#cameraTitle, QLabel#primaryCameraMarker {{
                background: transparent;
                color: {theme.text_primary};
                font-size: 13px;
                font-weight: 600;
            }}
            QLabel#primaryCameraMarker {{ color: {theme.primary}; }}
            QLabel#cameraMetrics {{
                background: transparent;
                color: {theme.text_secondary};
                font-size: 12px;
            }}
            """
        )
        for display in self._displays.values():
            display.apply_theme(theme)

    def show_message_all(self, message: str) -> None:
        if self._video_gate is not None:
            self._video_gate.invalidate("picture_unavailable")
        for cam, display in self._displays.items():
            display.show_message(message)
            self.set_camera_stats(cam, 0, None)

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

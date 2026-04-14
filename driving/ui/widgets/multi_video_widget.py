"""
多路视频布局组件

支持三种布局模式：
- front_dominant: 前视主导，前视占上半部分，下方左/后/右三等分
- triple: 三联屏，左/前/右 + 后视画中画
- grid: 2×2 等分网格
"""

from typing import Optional

from PyQt6.QtWidgets import (
    QFrame,
    QWidget,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QSizePolicy,
)
from PyQt6.QtCore import QTimer

from ..components import VideoDisplay
from infrastructure.video import FrameBuffer
from logger import get_logger

logger = get_logger(__name__)

# 布局模式常量
LAYOUT_FRONT_DOMINANT = "front_dominant"
LAYOUT_TRIPLE = "triple"
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


class MultiVideoWidget(QWidget):

    def __init__(self, fb: FrameBuffer, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._fb = fb
        self._layout_mode: Optional[str] = None

        # 四路 VideoDisplay
        self._displays = {}
        for cam in CAMERA_NAMES:
            d = VideoDisplay(scaling_mode="ultra_low_latency")
            d.show_message(f"{DEFAULT_LABELS[cam]} - 等待视频流...")
            self._displays[cam] = d

        # 后视画中画引用 (triple 布局专用)
        self._pip_back: Optional[VideoDisplay] = None

        # 主布局容器
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

        # 默认构建前视主导布局
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
    #  布局构建（每次切换时重建）
    # ------------------------------------------------------------------ #

    def _clear_layout(self) -> None:
        """清空主布局中的所有子控件"""
        while self._root_layout.count():
            item = self._root_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        # 清除画中画
        if self._pip_back is not None:
            self._pip_back.setParent(None)
            self._pip_back.deleteLater()
            self._pip_back = None

    def _apply_layout(self, mode: str) -> None:
        """根据模式重建布局"""
        self._clear_layout()

        # 外层带边框的容器
        container = QWidget()
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        container.setStyleSheet(_CONTAINER_STYLE)

        # 为每个 VideoDisplay 创建新的边框 frame
        frames = {}
        for cam in CAMERA_NAMES:
            frames[cam] = self._wrap_in_frame(self._displays[cam])

        if mode == LAYOUT_FRONT_DOMINANT:
            self._build_front_dominant(container, frames)
        elif mode == LAYOUT_TRIPLE:
            self._build_triple(container, frames)
        else:
            self._build_grid(container, frames)

        self._root_layout.addWidget(container)
        self._layout_mode = mode

    def _build_front_dominant(self, container: QWidget, frames: dict) -> None:
        """前视主导布局：前视占上方约 62%，下方三等分显示左/后/右"""
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        layout.addWidget(frames["front"], stretch=3)

        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(2)
        for cam in ("left", "back", "right"):
            bottom_layout.addWidget(frames[cam])

        layout.addWidget(bottom, stretch=2)

    def _build_triple(self, container: QWidget, frames: dict) -> None:
        """三联屏布局：左 / 前 / 右 + 后视画中画叠加在前视上方"""
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        for cam in ("left", "front", "right"):
            layout.addWidget(frames[cam])

        # 后视画中画
        pip = VideoDisplay(scaling_mode="ultra_low_latency")
        pip.show_message("后视 - 等待视频流...")
        pip.setFixedSize(240, 135)
        pip.setStyleSheet(_CELL_STYLE)
        pip.setParent(frames["front"])
        pip.move(10, 10)
        pip.raise_()
        pip.show()
        self._pip_back = pip

    def _build_grid(self, container: QWidget, frames: dict) -> None:
        """2×2 等分网格布局"""
        layout = QGridLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        order = [("front", 0, 0), ("back", 0, 1), ("left", 1, 0), ("right", 1, 1)]
        for cam, row, col in order:
            layout.addWidget(frames[cam], row, col)

    # ------------------------------------------------------------------ #
    #  布局切换
    # ------------------------------------------------------------------ #

    def set_layout(self, mode: str) -> None:
        """切换布局模式"""
        if mode == self._layout_mode:
            return
        self._apply_layout(mode)

    # ------------------------------------------------------------------ #
    #  帧刷新
    # ------------------------------------------------------------------ #

    def _refresh_frames(self) -> None:
        """从 FrameBuffer 拉取最新帧并更新各 VideoDisplay"""
        for cam in CAMERA_NAMES:
            frame = self._fb.get(cam)
            if frame is not None:
                self._displays[cam].update_frame(frame)

        if self._layout_mode == LAYOUT_TRIPLE and self._pip_back is not None:
            back_frame = self._fb.get("back")
            if back_frame is not None:
                self._pip_back.update_frame(back_frame)


    def apply_theme(self, theme) -> None:
        """为所有 VideoDisplay 应用主题"""
        for d in self._displays.values():
            d.apply_theme(theme)
        if self._pip_back is not None:
            self._pip_back.apply_theme(theme)

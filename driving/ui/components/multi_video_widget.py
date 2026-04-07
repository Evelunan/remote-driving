import numpy as np
import cv2

from PyQt6.QtWidgets import QWidget, QLabel, QGridLayout, QVBoxLayout
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage, QPixmap


class MultiVideoWidget(QWidget):
    """4路视频显示组件（支持多布局）"""

    def __init__(self, frame_buffer, parent=None):
        super().__init__(parent)
        self.fb = frame_buffer

        # 当前布局模式
        self.layout_mode = "grid"  # grid / focus / single
        self.focus_position = "front"

        # UI
        self.labels = {
            "front": QLabel(),
            "back": QLabel(),
            "left": QLabel(),
            "right": QLabel(),
        }

        for lbl in self.labels.values():
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background-color: black;")

        self._init_ui()

        # 刷新定时器
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frames)
        self.timer.start(30)  # ~30 FPS

    # =========================
    # UI 初始化
    # =========================
    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.setLayout(self.main_layout)
        self._apply_layout()

    def _clear_layout(self):
        while self.main_layout.count():
            item = self.main_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

    def _apply_layout(self):
        self._clear_layout()

        if self.layout_mode == "grid":
            self._grid_layout()

        elif self.layout_mode == "focus":
            self._focus_layout()

        elif self.layout_mode == "single":
            self._single_layout()

    # =========================
    # 布局模式
    # =========================

    def _grid_layout(self):
        """2x2布局"""
        grid = QGridLayout()

        grid.addWidget(self.labels["front"], 0, 0)
        grid.addWidget(self.labels["back"], 0, 1)
        grid.addWidget(self.labels["left"], 1, 0)
        grid.addWidget(self.labels["right"], 1, 1)

        self.main_layout.addLayout(grid)

    def _focus_layout(self):
        """主画面 + 小窗（远程驾驶常用）"""
        grid = QGridLayout()

        # 主画面（占大区域）
        main = self.labels[self.focus_position]
        grid.addWidget(main, 0, 0, 2, 2)

        # 其他作为小窗
        small_positions = [p for p in self.labels if p != self.focus_position]

        grid.addWidget(self.labels[small_positions[0]], 0, 2)
        grid.addWidget(self.labels[small_positions[1]], 1, 2)
        grid.addWidget(self.labels[small_positions[2]], 2, 2)

        self.main_layout.addLayout(grid)

    def _single_layout(self):
        """单画面"""
        self.main_layout.addWidget(self.labels[self.focus_position])

    # =========================
    # 外部接口
    # =========================

    def set_layout_mode(self, mode: str):
        """切换布局"""
        self.layout_mode = mode
        self._apply_layout()

    def set_focus(self, position: str):
        """设置主画面"""
        self.focus_position = position
        if self.layout_mode in ["focus", "single"]:
            self._apply_layout()

    # =========================
    # 渲染
    # =========================

    def update_frames(self):
        frames = self.fb.all_frames

        for pos, frame in zip(["front", "back", "left", "right"], frames):
            self._update_label(self.labels[pos], frame)

    def _update_label(self, label: QLabel, frame: np.ndarray):
        if frame is None:
            label.clear()
            return

        img = self._to_qimage(frame)
        label.setPixmap(QPixmap.fromImage(img).scaled(
            label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation
        ))

    # =========================
    # 工具函数
    # =========================

    def _to_qimage(self, frame: np.ndarray):
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        return QImage(
            frame.data, w, h, bytes_per_line,
            QImage.Format.Format_BGR888
        )
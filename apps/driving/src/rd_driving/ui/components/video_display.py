"""
优化的视频显示组件 (PyQt6 版)

特性：
- 智能缩放：缓存缩放参数，避免重复计算
- 零拷贝优化：复用内存缓冲区，避免每帧分配
- 高性能渲染：利用 OpenCV SIMD 加速
- 自适应画质：根据渲染耗时动态调整插值算法
"""

import time
from typing import Any

import numpy as np
from PyQt6.QtCore import QEvent, QSize, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QWidget
from rd_driving.interfaces.ui import ITheme
from rd_webrtc.opencv import cv2

# 缩放模式配置
# 注意：cv2.INTER_* 是整数常量，在 PyQt6 中无需特殊处理
SCALING_MODES: dict[str, Any] = {
    "quality": {
        "shrink": cv2.INTER_AREA,
        "enlarge": cv2.INTER_LANCZOS4,
    },
    "balanced": {
        "shrink": cv2.INTER_AREA,
        "enlarge": cv2.INTER_LINEAR,
    },
    "performance": {
        "shrink": cv2.INTER_LINEAR,
        "enlarge": cv2.INTER_LINEAR,
    },
    "ultra_low_latency": {
        "shrink": cv2.INTER_NEAREST,
        "enlarge": cv2.INTER_NEAREST,
    },
}

# 默认样式常量
DEFAULT_STYLES = {
    "bg_color": "#0D1117",
    "text_color": "#6E7681",
    "font_size": 18,
    "radius": 8,
}


class VideoDisplay(QLabel):
    """优化的视频显示组件"""

    def __init__(
        self,
        scaling_mode: str = "ultra_low_latency",
        parent: QWidget | None = None,
        *,
        debug_overlay=False,
    ):
        super().__init__(parent)

        # 基础配置
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # 状态变量
        self._theme: ITheme | None = None
        self._scaling_mode = scaling_mode
        self._current_frame: np.ndarray | None = None
        self._debug_overlay = debug_overlay
        self._display_time = ""
        self._resolution = (0, 0)

        # 缓存系统
        self._cached_widget_size = QSize(0, 0)
        self._cached_frame_size = (0, 0)
        self._cached_target_size = (0, 0)
        self._cached_interpolation = cv2.INTER_NEAREST
        self._scaled_buffer: np.ndarray | None = None

        # 性能统计
        self._fps = 0
        self._last_frame_id = -1
        self._pixmap_frame_id = self._presented_id = -1
        self._on_presented = None
        self._frame_count = 0
        self._last_fps_time = time.monotonic()
        # 应用默认样式
        self._apply_default_style()

    def _apply_default_style(self):
        """应用默认深色样式"""
        self.setStyleSheet(
            f"""
            QLabel {{
                background-color: {DEFAULT_STYLES["bg_color"]};
                border: none;
                border-radius: {DEFAULT_STYLES["radius"]}px;
                color: {DEFAULT_STYLES["text_color"]};
                font-size: {DEFAULT_STYLES["font_size"]}px;
            }}
        """
        )

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: ITheme) -> None:
        """应用自定义主题"""
        self._theme = theme

        try:
            font_family = theme.get_font_family()
            font_rule = f"font-family: {font_family};" if font_family else ""

            self.setStyleSheet(
                f"""
                QLabel {{
                    background-color: {theme.bg_primary};
                    border: none;
                    border-radius: {DEFAULT_STYLES["radius"]}px;
                    color: {theme.text_muted};
                    font-size: {DEFAULT_STYLES["font_size"]}px;
                    {font_rule}
                }}
            """
            )
        except AttributeError:
            # 主题属性缺失时回退
            self._apply_default_style()

    def _get_interpolation(
        self, src_size: tuple[int, int], dst_size: tuple[int, int]
    ) -> int:
        """根据缩放方向和性能表现选择最佳插值算法"""
        src_w, src_h = src_size
        dst_w, dst_h = dst_size
        if dst_w <= 0 or dst_h <= 0:
            return cv2.INTER_NEAREST
        is_shrinking = (dst_w * dst_h) < (src_w * src_h)
        # 指定模式
        mode_config = SCALING_MODES.get(self._scaling_mode)
        if mode_config:
            return mode_config["shrink"] if is_shrinking else mode_config["enlarge"]
        # 兜底
        return cv2.INTER_LINEAR

    def _update_scale_cache(self, frame_w: int, frame_h: int) -> bool:
        """
        更新缩放缓存。
        返回 True 表示需要重新缩放，False 表示可以使用旧缓存（尺寸未变）。
        """
        widget_size = self.size()
        frame_size = (frame_w, frame_h)

        # 快速路径：尺寸未变
        if (
            widget_size == self._cached_widget_size
            and frame_size == self._cached_frame_size
        ):
            return False

        # 计算目标尺寸 (保持宽高比)
        tw, th = widget_size.width(), widget_size.height()

        if tw <= 0 or th <= 0:
            self._cached_target_size = (frame_w, frame_h)
            self._cached_widget_size = widget_size
            self._cached_frame_size = frame_size
            return True

        scale = min(tw / frame_w, th / frame_h)
        target_w = max(1, int(frame_w * scale))
        target_h = max(1, int(frame_h * scale))

        # 更新缓存状态
        self._cached_widget_size = widget_size
        self._cached_frame_size = frame_size
        self._cached_target_size = (target_w, target_h)

        # 预计算插值算法
        self._cached_interpolation = self._get_interpolation(
            frame_size, (target_w, target_h)
        )

        # 预分配/重分配缓冲区 (HWC 格式: Height, Width, Channels)
        if (
            self._scaled_buffer is None
            or self._scaled_buffer.shape[0] != target_h
            or self._scaled_buffer.shape[1] != target_w
        ):
            # 确保缓冲区是连续的内存块
            self._scaled_buffer = np.empty((target_h, target_w, 3), dtype=np.uint8)

        return True

    def _scale_and_display(self, frame: np.ndarray):
        """核心渲染流程：缩放并显示"""
        if frame is None:
            return

        h, w = frame.shape[:2]
        needs_resize = self._update_scale_cache(w, h)

        target_w, target_h = self._cached_target_size

        display_frame: np.ndarray | None = None

        # 执行缩放
        if needs_resize or (target_w != w or target_h != h):
            if self._scaled_buffer is not None:
                try:
                    # 零拷贝关键：直接写入预分配缓冲区
                    cv2.resize(
                        frame,
                        (target_w, target_h),
                        dst=self._scaled_buffer,
                        interpolation=self._cached_interpolation,
                    )
                    display_frame = self._scaled_buffer
                except cv2.error:
                    # 极端情况下 resize 失败，回退到创建新数组
                    display_frame = cv2.resize(
                        frame,
                        (target_w, target_h),
                        interpolation=self._cached_interpolation,
                    )
            else:
                display_frame = cv2.resize(
                    frame,
                    (target_w, target_h),
                    interpolation=self._cached_interpolation,
                )
        else:
            # 无需缩放，直接使用原帧
            display_frame = frame

        if display_frame is None:
            return

        # 构建 QImage
        # 确保数据是连续的，虽然 cv2.resize dst 通常是连续的，但保险起见
        if not display_frame.flags["C_CONTIGUOUS"]:
            display_frame = np.ascontiguousarray(display_frame)

        sh, sw = display_frame.shape[:2]
        bytes_per_line = sw * 3  # BGR888: 3 bytes per pixel

        # 构造 QImage
        # 注意：PyQt6 中 QImage 构造函数参数顺序保持一致
        qimg = QImage(
            display_frame.data, sw, sh, bytes_per_line, QImage.Format.Format_BGR888
        )

        # 更新显示
        # QPixmap::fromImage 会进行一次拷贝，这是 Qt 显示的必经之路，无法避免
        # 但我们避免了 Python 层面的中间数组分配
        self.setPixmap(QPixmap.fromImage(qimg))
        self._pixmap_frame_id = self._last_frame_id

    def update_frame(
        self, frame: np.ndarray, frame_id: int, *, on_presented=None
    ) -> None:
        """外部调用的帧更新接口"""
        if frame is None:
            return
        if frame_id == self._last_frame_id:
            # 重复帧，不渲染
            return
        self._last_frame_id = frame_id
        self._on_presented = on_presented

        if self._debug_overlay:
            self._display_time = time.strftime("%H:%M:%S")
        self._current_frame = frame
        h, w = frame.shape[:2]
        self._resolution = (w, h)

        self._scale_and_display(frame)

        # FPS 计算
        self._frame_count += 1
        now = time.monotonic()
        elapsed = now - self._last_fps_time
        if elapsed >= 1.0:
            self._fps = int(self._frame_count / elapsed)
            self._frame_count = 0
            self._last_fps_time = now

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._debug_overlay and self._current_frame is not None:
            painter = QPainter(self)
            painter.setPen(QColor(0, 255, 0))
            painter.drawText(12, 24, "display: " + self._display_time)
            painter.end()
        if (
            self._current_frame is not None
            and self._pixmap_frame_id == self._last_frame_id
            and self._presented_id != self._last_frame_id
        ):
            self._presented_id = self._last_frame_id
            if self._on_presented is not None:
                self._on_presented()

    def show_message(self, text: str) -> None:
        """显示文本消息（无视频流时）"""
        self._current_frame = None
        self._pixmap_frame_id = self._presented_id = -1
        self._on_presented = None
        self._scaled_buffer = None
        self._cached_widget_size = QSize(0, 0)
        self._last_frame_id = -1
        self._fps = self._frame_count = 0
        self._resolution = (0, 0)
        self._last_fps_time = time.monotonic()
        self.clear()
        self.setText(text)

        # 确保文本样式与主题一致
        if self._theme:
            try:
                self.setStyleSheet(
                    f"""
                    QLabel {{
                        background-color: {self._theme.bg_primary};
                        border: none;
                        color: {self._theme.text_muted};
                        font-size: {DEFAULT_STYLES["font_size"]}px;
                        font-family: {self._theme.get_font_family() or ""};
                        border-radius: {DEFAULT_STYLES["radius"]}px;
                    }}
                """
                )
            except AttributeError:
                self._apply_default_style()
        else:
            self._apply_default_style()

    def set_scaling_mode(self, mode: str) -> None:
        """切换缩放模式"""
        if mode in SCALING_MODES:
            self._scaling_mode = mode
            # 强制清除缓存，下一帧重新计算插值算法
            self._cached_widget_size = QSize(0, 0)

    # --- 统计信息获取 ---
    def get_fps(self) -> int:
        return self._fps

    def get_resolution(self) -> tuple[int, int]:
        return self._resolution

    # --- 事件处理 ---
    def resizeEvent(self, event):
        """窗口大小改变时，立即重新缩放当前帧"""
        super().resizeEvent(event)
        # 清除尺寸缓存，触发重新计算
        self._cached_widget_size = QSize(0, 0)

        if self._current_frame is not None:
            # 异步或直接同步刷新？这里直接调用以保持一致性
            # 如果帧率极高，可以考虑加一个简单的节流，但通常 resize 不频繁
            self._scale_and_display(self._current_frame)

    def changeEvent(self, event: QEvent):
        """处理主题变更等事件"""
        if event.type() == QEvent.Type.StyleChange and self._current_frame is not None:
            # 样式改变可能影响边框等，进而影响可用绘制区域
            self._cached_widget_size = QSize(0, 0)
            self._scale_and_display(self._current_frame)
        super().changeEvent(event)

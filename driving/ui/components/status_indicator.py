"""
状态指示器组件 (PyQt6 优化版)

带发光效果的圆形状态指示灯，支持高 DPI 渲染和径向渐变光晕。
"""

from typing import Optional
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor, QBrush, QRadialGradient, QPen
from PyQt6.QtCore import Qt, QPointF

from interfaces.ui import ITheme

# 默认颜色常量
DEFAULT_COLORS = {
    "active": "#3FB950",      # 绿色
    "inactive": "#6E7681",    # 灰色
}

# 光晕配置
GLOW_CONFIG = {
    "alpha": 80,              # 光晕透明度 (0-255)
    "spread_factor": 1.8,     # 光晕相对于核心圆点的扩散倍数
}

class StatusIndicator(QWidget):
    """状态指示灯"""

    def __init__(self, size: int = 10, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._size = size
        self._active = False
        self._theme: Optional[ITheme] = None
        
        # 初始化尺寸 (考虑高 DPI)
        self._update_fixed_size()
        
        # 可选：设置工具提示以便无障碍访问
        self.update_tooltip()

    def _update_fixed_size(self):
        """根据逻辑大小和 DPI 更新控件的物理固定大小"""
        # 获取设备的像素比率 (例如 Retina 屏可能是 2.0)
        ratio = self.devicePixelRatio()
        # 基础边距 (逻辑像素)
        margin = 3 
        total_logical_size = self._size + (margin * 2)
        
        # 设置固定大小为物理像素，但 Qt 会自动处理逻辑坐标映射
        # 这里我们直接设置逻辑大小，Qt 的 paintEvent 会根据 ratio 自动缩放
        # 但为了确保布局管理器预留足够空间，我们通常设置逻辑大小即可
        # 如果需要在高分屏下显得更大，可以手动乘以 ratio，但通常 Qt 布局系统会处理好
        self.setFixedSize(int(total_logical_size), int(total_logical_size))

    def update_tooltip(self):
        """根据状态更新工具提示"""
        if self._active:
            self.setToolTip("在线")
        else:
            self.setToolTip("离线")

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: ITheme) -> None:
        """应用主题并刷新绘制"""
        self._theme = theme
        self.update()

    def set_size(self, size: int) -> None:
        """动态设置指示灯大小"""
        if self._size != size:
            self._size = size
            self._update_fixed_size()
            self.update()

    def set_active(self, active: bool) -> None:
        """设置活跃状态"""
        if self._active != active:
            self._active = active
            self.update_tooltip()
            self.update()

    def is_active(self) -> bool:
        return self._active

    def _get_colors(self) -> tuple[str, str]:
        """获取当前主题下的颜色，带容错处理"""
        if self._theme:
            try:
                active_color = self._theme.online
                inactive_color = self._theme.text_muted
                return active_color, inactive_color
            except AttributeError:
                # 主题属性缺失，回退到默认
                pass
        
        return DEFAULT_COLORS["active"], DEFAULT_COLORS["inactive"]

    def paintEvent(self, event):
        painter = QPainter(self)
        # 开启抗锯齿，确保圆形边缘平滑
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # 关闭文本抗锯齿（虽然这里不画文字，但是个好习惯）
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)

        rect = self.rect()
        center = QPointF(rect.center().x(), rect.center().y())
        
        # 计算半径 (逻辑像素)
        # 固定大小是 size + 6 (3px margin * 2)，所以半径大约是 (size+6)/2
        # 核心圆点半径
        core_radius = self._size / 2.0
        # 外边距
        margin = 3.0
        
        active_color_str, inactive_color_str = self._get_colors()
        current_color = QColor(active_color_str if self._active else inactive_color_str)

        # --- 绘制光晕 (仅当激活时) ---
        if self._active:
            glow_radius = core_radius * GLOW_CONFIG["spread_factor"]
            
            # 使用径向渐变创建柔和的光晕
            gradient = QRadialGradient(center, glow_radius)
            glow_color = QColor(active_color_str)
            glow_color.setAlpha(GLOW_CONFIG["alpha"])
            
            gradient.setColorAt(0.0, glow_color)
            gradient.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0)) # 边缘完全透明
            
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, glow_radius, glow_radius)

        # --- 绘制核心圆点 ---
        painter.setBrush(QBrush(current_color))
        painter.setPen(Qt.PenStyle.NoPen) # 无边框
        
        # 绘制主圆
        painter.drawEllipse(center, core_radius, core_radius)
        
        # (可选) 添加一个极细的高光点，增加立体感
        # highlight_pos = QPointF(center.x() - core_radius/3, center.y() - core_radius/3)
        # highlight_radius = core_radius / 4
        # painter.setBrush(QBrush(QColor(255, 255, 255, 100)))
        # painter.drawEllipse(highlight_pos, highlight_radius, highlight_radius)

    def resizeEvent(self, event):
        # 如果控件被外部强制拉伸，可能需要重新计算内部绘制逻辑
        # 但因为我们设置了 setFixedSize，通常不会触发此事件，除非父布局强制拉伸
        super().resizeEvent(event)
        # 如果需要自适应大小而不是固定大小，可以在这里重新计算 _size
        # 但当前设计倾向于固定大小以保持一致性
"""
视频面板组件 (PyQt6 优化版)

左侧视频显示面板，包含视频区域和智能覆盖层。
支持真正的半透明覆盖（视频与提示共存）、动态状态管理及主题自适应。
"""

from typing import Optional, Tuple
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QStackedWidget, QSizePolicy, QWidget, QGraphicsOpacityEffect
from PyQt6.QtCore import Qt, QRect, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor, QPalette

from interfaces.ui import ITheme, IVideoDisplay
from ..components import VideoDisplay

# 默认颜色兜底
DEFAULT_COLORS = {
    "bg_secondary": "#161B22",
    "border": "#30363D",
    "bg_primary": "#0D1117",
    "text_secondary": "#8B949E",
    "error": "#DA3633",
    "success": "#238636",
    "warning": "#D29922"
}

class VideoPanel(QFrame):
    """视频面板 - 支持视频流显示与智能覆盖层"""

    def __init__(self, scaling_mode: str = "ultra_low_latency", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme: Optional[ITheme] = None
        self._scaling_mode = scaling_mode

        # 尺寸策略：优先扩展以填充可用空间
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)
        self.setObjectName("videoPanel")

        self._setup_ui()

    def _setup_ui(self):
        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        # === 视频容器 (使用 QStackedWidget 以便未来扩展多路视频) ===
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent; border: none;")
        
        # 视频显示组件
        self._video = VideoDisplay(self._scaling_mode)
        self._video.show_message("等待视频流...")
        self._stack.addWidget(self._video)
        
        # 默认显示视频页
        self._stack.setCurrentIndex(0)
        
        layout.addWidget(self._stack)

        # === 智能覆盖层 (绝对定位悬浮) ===
        # 不再使用 QStackedWidget 切换，而是作为子控件悬浮，实现真正的 Overlay
        self._overlay = QWidget(self)
        self._overlay.setObjectName("videoOverlay")
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False) # 默认拦截鼠标
        
        # 覆盖层布局
        overlay_layout = QVBoxLayout(self._overlay)
        overlay_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.setContentsMargins(0, 0, 0, 0)

        self._overlay_icon = QLabel()
        self._overlay_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay_icon.setStyleSheet("font-size: 32px; border: none; margin-bottom: 8px;")
        overlay_layout.addWidget(self._overlay_icon)

        self._overlay_text = QLabel()
        self._overlay_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay_text.setStyleSheet("font-size: 16px; font-weight: 500; border: none;")
        self._overlay_text.setWordWrap(True)
        overlay_layout.addWidget(self._overlay_text)

        # 初始隐藏
        self._overlay.hide()
        
        # 简单的淡入淡出效果
        self._opacity_effect = QGraphicsOpacityEffect(self._overlay)
        self._overlay.setGraphicsEffect(self._opacity_effect)
        self._fade_animation = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_animation.setDuration(200)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: ITheme) -> None:
        """应用主题样式"""
        self._theme = theme

        # 辅助获取颜色
        def get_color(key: str, default: str) -> str:
            try:
                val = getattr(theme, key)
                return val if val else default
            except AttributeError:
                return default

        bg_secondary = get_color('bg_secondary', DEFAULT_COLORS['bg_secondary'])
        border = get_color('border', DEFAULT_COLORS['border'])
        bg_primary = get_color('bg_primary', DEFAULT_COLORS['bg_primary'])
        text_secondary = get_color('text_secondary', DEFAULT_COLORS['text_secondary'])

        # 1. 面板主体
        self.setStyleSheet(f"""
            #videoPanel {{
                background-color: {bg_secondary};
                border: 1px solid {border};
                border-radius: 12px;
            }}
        """)

        # 2. 视频组件
        if hasattr(self._video, 'apply_theme'):
            self._video.apply_theme(theme)

        # 3. 覆盖层样式
        rgb_val = self._hex_to_rgb(bg_primary)
        self._overlay.setStyleSheet(f"""
            #videoOverlay {{
                background-color: rgba({rgb_val}, 0.85);
                border: none;
                border-radius: 8px; /* 略小于父容器 */
            }}
        """)

        self._overlay_text.setStyleSheet(f"""
            color: {text_secondary};
            font-size: 16px;
            font-weight: 500;
            border: none;
            font-family: {self._get_font_family(theme)};
        """)
        
        # 如果当前正在显示覆盖层，刷新其图标颜色等
        if not self._overlay.isHidden():
            self._refresh_overlay_style()

    def _get_font_family(self, theme: ITheme) -> str:
        try:
            fam = theme.get_font_family()
            return fam if fam else "Segoe UI, sans-serif"
        except AttributeError:
            return "Segoe UI, sans-serif"

    def get_video_display(self) -> IVideoDisplay:
        return self._video

    def show_overlay(self, text: str, icon: str = "", fade: bool = True) -> None:
        """显示覆盖层
        
        Args:
            text: 显示文本
            icon: 可选的图标字符 (如 ●, ⚠, ✖)
            fade: 是否使用淡入动画
        """
        self._overlay_text.setText(text)
        self._overlay_icon.setText(icon)
        self._overlay_icon.setVisible(bool(icon))
        
        self._refresh_overlay_style()
        
        self._overlay.raise_() # 确保在最上层
        self._overlay.show()
        
        if fade:
            self._fade_animation.setStartValue(0.0)
            self._fade_animation.setEndValue(1.0)
            self._fade_animation.start()
        else:
            self._opacity_effect.setOpacity(1.0)

    def hide_overlay(self, fade: bool = True) -> None:
        """隐藏覆盖层
        
        Args:
            fade: 是否使用淡出动画
        """
        if self._overlay.isHidden():
            return
            
        if fade:
            self._fade_animation.setStartValue(1.0)
            self._fade_animation.setEndValue(0.0)
            self._fade_animation.finished.connect(self._overlay.hide)
            # 防止多次连接
            try:
                self._fade_animation.finished.disconnect(self._overlay.hide)
            except TypeError:
                pass
            self._fade_animation.finished.connect(self._overlay.hide)
            self._fade_animation.start()
        else:
            self._overlay.hide()
            self._opacity_effect.setOpacity(1.0)

    def set_status(self, status: str, message: Optional[str] = None) -> None:
        """便捷方法：根据状态类型显示不同的覆盖层
        
        Status types:
            - "loading": 显示加载图标
            - "error": 显示错误图标和红色文字
            - "offline": 显示离线图标
            - "success": 显示成功图标
            - "custom": 显示自定义文本
        """
        icons = {
            "loading": "⟳",
            "error": "⚠",
            "offline": "✖",
            "success": "✔",
            "custom": ""
        }
        
        default_messages = {
            "loading": "正在加载视频流...",
            "error": "视频流连接失败",
            "offline": "设备已离线",
            "success": "连接成功",
            "custom": message or ""
        }

        icon = icons.get(status, "")
        text = message if status == "custom" else default_messages.get(status, "")
        
        self.show_overlay(text, icon)
        
        # 特殊状态样式调整
        if status == "error" and self._theme:
            try:
                error_color = self._theme.error if hasattr(self._theme, 'error') else DEFAULT_COLORS['error']
                self._overlay_text.setStyleSheet(f"color: {error_color}; font-size: 16px; font-weight: bold; border: none;")
            except:
                pass

    def _refresh_overlay_style(self):
        """根据当前主题刷新覆盖层内部样式"""
        if not self._theme:
            return
        # 这里可以根据需要动态改变图标颜色等
        # 目前主要依赖 CSS，如果需要更复杂的动态样式可在此扩展

    def resizeEvent(self, event):
        """调整覆盖层大小以匹配父容器"""
        super().resizeEvent(event)
        # 覆盖层应该比父容器稍小一点以露出圆角边框，或者完全覆盖内部内容区
        # 这里选择完全覆盖内容区 (减去 layout margins)
        # 简单做法：直接设为父控件大小，利用 border-radius 裁剪
        self._overlay.setGeometry(self.rect())
        
        # 如果父容器有边框，覆盖层圆角应略小
        # 这里假设父容器 border-radius 12px, overlay 设为 12px 即可，因为背景是半透明的

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> str:
        """十六进制转 RGB 字符串 (r, g, b)"""
        hex_color = hex_color.lstrip('#')
        if len(hex_color) != 6:
            return "13, 17, 23" # 默认深色
        
        try:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return f"{r}, {g}, {b}"
        except ValueError:
            return "13, 17, 23"

    def set_mouse_passthrough(self, enabled: bool) -> None:
        """设置鼠标事件是否穿透覆盖层
        
        Args:
            enabled: True 为穿透（不拦截鼠标），False 为拦截
        """
        if enabled:
            self._overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        else:
            self._overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
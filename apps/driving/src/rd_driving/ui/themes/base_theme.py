"""
主题基类

提供主题系统的抽象基类，定义颜色属性和样式方法的默认实现。
"""

import platform
from abc import ABC

from rd_driving.interfaces.ui import ITheme


def get_system_font() -> str:
    """获取系统默认字体"""
    system = platform.system()
    if system == "Windows":
        return "Microsoft YaHei"
    elif system == "Darwin":
        return "PingFang SC"
    else:
        return "Noto Sans CJK SC"


class BaseTheme(ITheme, ABC):
    """驾驶台与会话页共用的哑光表面和状态色。"""

    bg_primary = "#101820"
    bg_secondary = "#18242E"
    bg_tertiary = "#23323E"
    button_hover = "#354C5D"
    bg_card = bg_secondary
    text_primary = "#EDF4F8"
    text_secondary = "#CAD8E2"
    text_muted = "#AABBC8"
    border = "#405463"
    success = "#82D6AA"
    warning = "#F5CB80"
    error = "#FF9B94"
    online = success
    danger = error
    danger_dark = "#EB807B"

    def __init__(self):
        self._font_family = get_system_font()

    def get_font_family(self) -> str:
        return self._font_family

    def get_card_style(self) -> str:
        """默认卡片样式"""
        return f"""
            QFrame {{
                background-color: {self.bg_card};
                border: 1px solid {self.border};
                border-radius: 6px;
            }}
        """

    def get_button_style(self, *, compact: bool = False, emphasized: bool = True) -> str:
        """所有按钮共用交互颜色，尺寸和字重区分操作层级。"""
        return f"""
            QPushButton {{
                background-color: {self.bg_tertiary};
                color: {self.text_primary};
                border: 2px solid {self.border};
                border-radius: 5px;
                font-size: {12 if compact else 13}px;
                font-weight: {600 if emphasized else 400};
                font-family: "{self._font_family}";
                padding: {"0 4px" if compact else "6px 14px"};
            }}
            QPushButton:enabled:hover {{
                background-color: {self.button_hover};
                border-color: {self.primary};
            }}
            QPushButton:enabled:checked {{
                background-color: {self.primary};
                color: {self.bg_primary};
                border-color: {self.primary};
            }}
            QPushButton:enabled:checked:hover {{
                background-color: {self.primary_glow};
            }}
            QPushButton:enabled:pressed, QPushButton:enabled:checked:pressed {{
                background-color: {self.primary_dark};
                color: {self.bg_primary};
                border-color: {self.primary_dark};
            }}
            QPushButton:enabled:focus {{
                border: 2px dashed {self.text_primary};
            }}
            QPushButton:enabled:checked:focus, QPushButton:enabled:pressed:focus {{
                border: 2px dashed {self.bg_primary};
            }}
            QPushButton:disabled {{
                background-color: {self.bg_primary};
                color: {self.text_muted};
                border-color: {self.bg_tertiary};
            }}
        """

    def get_secondary_button_style(self) -> str:
        return self.get_button_style(emphasized=False)

    def get_navbar_style(self) -> str:
        """默认导航栏样式"""
        return f"""
            QFrame {{
                background-color: {self.bg_secondary};
                border-bottom: 1px solid {self.border};
            }}
        """

    def get_scrollbar_style(self) -> str:
        """默认滚动条样式"""
        return f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                background: {self.bg_secondary};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {self.border};
                border-radius: 4px;
                min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {self.text_muted};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """

    def get_glass_effect_style(self) -> str:
        return self.get_card_style()

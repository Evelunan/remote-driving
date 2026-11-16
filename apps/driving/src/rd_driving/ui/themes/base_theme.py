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
    """主题基类 - 提供通用实现"""

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
                border-radius: 10px;
            }}
        """

    def get_button_style(self) -> str:
        """默认按钮样式"""
        return f"""
            QPushButton {{
                background-color: {self.primary};
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
                font-family: {self._font_family};
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background-color: {self.primary_dark};
            }}
            QPushButton:pressed {{
                background-color: {self.primary};
            }}
        """

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
                width: 6px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical {{
                background: {self.border};
                border-radius: 3px;
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """

    def get_glass_effect_style(self) -> str:
        """默认玻璃效果样式 - 子类可重写"""
        return f"""
            QFrame {{
                background-color: rgba(22, 27, 34, 0.85);
                border: 1px solid rgba(48, 54, 61, 0.6);
                border-radius: 12px;
            }}
        """

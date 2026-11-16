"""
UI 组件工厂

提供 UI 组件的统一创建接口，支持主题管理。
"""

from typing import Optional, Dict, Type

from rd_driving.interfaces.ui import ITheme
from rd_driving.ui.themes import BaseTheme, DarkTechTheme, ModernGlassTheme
from rd_driving.ui.components import (
    VideoDisplay, StatusIndicator, ConnectionItem,
    DataCard, StatItem
)
from rd_driving.ui.widgets import Navbar, ControlPanel, VideoPanel


class UIFactory:
    """UI 组件工厂"""

    # 可用主题
    _themes: Dict[str, Type[BaseTheme]] = {
        "dark_tech": DarkTechTheme,
        "modern_glass": ModernGlassTheme,
    }

    # 当前主题实例
    _current_theme: Optional[ITheme] = None

    @classmethod
    def set_theme(cls, theme_name: str) -> ITheme:
        """设置并返回主题"""
        theme_class = cls._themes.get(theme_name)
        if theme_class is None:
            raise ValueError(f"Unknown theme: {theme_name}. Available: {list(cls._themes.keys())}")

        cls._current_theme = theme_class()
        return cls._current_theme

    @classmethod
    def get_theme(cls) -> ITheme:
        """获取当前主题（如果未设置则使用默认主题）"""
        if cls._current_theme is None:
            cls._current_theme = DarkTechTheme()
        return cls._current_theme

    # ===== 基础组件创建 =====

    @staticmethod
    def create_video_display(scaling_mode: str = "balanced") -> VideoDisplay:
        """创建视频显示组件"""
        video = VideoDisplay(scaling_mode)
        theme = UIFactory.get_theme()
        video.apply_theme(theme)
        return video

    @staticmethod
    def create_status_indicator(size: int = 10) -> StatusIndicator:
        """创建状态指示器"""
        indicator = StatusIndicator(size)
        theme = UIFactory.get_theme()
        indicator.apply_theme(theme)
        return indicator

    @staticmethod
    def create_connection_item(name: str) -> ConnectionItem:
        """创建连接状态项"""
        item = ConnectionItem(name)
        theme = UIFactory.get_theme()
        item.apply_theme(theme)
        return item

    @staticmethod
    def create_data_card(title: str) -> DataCard:
        """创建数据卡片"""
        card = DataCard(title)
        theme = UIFactory.get_theme()
        card.apply_theme(theme)
        return card

    @staticmethod
    def create_stat_item(label: str, unit: str = "") -> StatItem:
        """创建统计数据项"""
        item = StatItem(label, unit)
        theme = UIFactory.get_theme()
        item.apply_theme(theme)
        return item

    # ===== 复合组件创建 =====

    @staticmethod
    def create_navbar() -> Navbar:
        """创建导航栏"""
        navbar = Navbar()
        theme = UIFactory.get_theme()
        navbar.apply_theme(theme)
        return navbar

    @staticmethod
    def create_control_panel() -> ControlPanel:
        """创建控制面板"""
        panel = ControlPanel()
        theme = UIFactory.get_theme()
        panel.apply_theme(theme)
        return panel

    @staticmethod
    def create_video_panel(scaling_mode: str = "balanced") -> VideoPanel:
        """创建视频面板"""
        panel = VideoPanel(scaling_mode)
        theme = UIFactory.get_theme()
        panel.apply_theme(theme)
        return panel

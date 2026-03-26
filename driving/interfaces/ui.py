"""
UI 组件接口定义

定义远程驾驶系统 UI 层的所有抽象接口，
实现面向接口编程，提高代码可扩展性和可测试性。
"""

from abc import ABC, abstractmethod
from typing import Optional
import numpy as np


class ITheme(ABC):
    """主题接口 - 定义颜色方案和组件样式"""

    # ===== 颜色属性 =====
    @property
    @abstractmethod
    def primary(self) -> str:
        """主色调"""
        pass

    @property
    @abstractmethod
    def primary_dark(self) -> str:
        """主色调深色"""
        pass

    @property
    @abstractmethod
    def primary_glow(self) -> str:
        """主色调发光色"""
        pass

    @property
    @abstractmethod
    def accent(self) -> str:
        """强调色"""
        pass

    @property
    @abstractmethod
    def accent_dark(self) -> str:
        """强调色深色"""
        pass

    @property
    @abstractmethod
    def danger(self) -> str:
        """危险/警告色"""
        pass

    @property
    @abstractmethod
    def danger_dark(self) -> str:
        """危险色深色"""
        pass

    @property
    @abstractmethod
    def bg_primary(self) -> str:
        """主背景色"""
        pass

    @property
    @abstractmethod
    def bg_secondary(self) -> str:
        """次要背景色"""
        pass

    @property
    @abstractmethod
    def bg_tertiary(self) -> str:
        """第三背景色"""
        pass

    @property
    @abstractmethod
    def bg_card(self) -> str:
        """卡片背景色"""
        pass

    @property
    @abstractmethod
    def text_primary(self) -> str:
        """主要文字颜色"""
        pass

    @property
    @abstractmethod
    def text_secondary(self) -> str:
        """次要文字颜色"""
        pass

    @property
    @abstractmethod
    def text_muted(self) -> str:
        """弱化文字颜色"""
        pass

    @property
    @abstractmethod
    def border(self) -> str:
        """边框颜色"""
        pass

    @property
    @abstractmethod
    def border_highlight(self) -> str:
        """高亮边框颜色"""
        pass

    @property
    @abstractmethod
    def success(self) -> str:
        """成功状态色"""
        pass

    @property
    @abstractmethod
    def warning(self) -> str:
        """警告状态色"""
        pass

    @property
    @abstractmethod
    def error(self) -> str:
        """错误状态色"""
        pass

    @property
    @abstractmethod
    def online(self) -> str:
        """在线状态色"""
        pass

    # ===== 样式方法 =====
    @abstractmethod
    def get_font_family(self) -> str:
        """获取字体族"""
        pass

    @abstractmethod
    def get_card_style(self) -> str:
        """获取卡片样式表"""
        pass

    @abstractmethod
    def get_button_style(self) -> str:
        """获取按钮样式表"""
        pass

    @abstractmethod
    def get_navbar_style(self) -> str:
        """获取导航栏样式表"""
        pass

    @abstractmethod
    def get_scrollbar_style(self) -> str:
        """获取滚动条样式表"""
        pass

    @abstractmethod
    def get_glass_effect_style(self) -> str:
        """获取玻璃拟态效果样式"""
        pass


class IUIComponent(ABC):
    """UI 组件基接口"""

    @abstractmethod
    def get_widget(self):
        """获取底层 Qt Widget"""
        pass

    @abstractmethod
    def apply_theme(self, theme: ITheme) -> None:
        """应用主题"""
        pass


class IVideoDisplay(IUIComponent):
    """视频显示组件接口"""

    @abstractmethod
    def update_frame(self, frame: np.ndarray) -> None:
        """更新显示帧"""
        pass

    @abstractmethod
    def get_fps(self) -> int:
        """获取当前帧率"""
        pass

    @abstractmethod
    def get_resolution(self) -> tuple[int, int]:
        """获取当前分辨率 (width, height)"""
        pass

    @abstractmethod
    def get_drop_rate(self) -> float:
        """获取丢帧率 (0.0-1.0)"""
        pass

    @abstractmethod
    def show_message(self, text: str) -> None:
        """显示消息（无视频时）"""
        pass

    @abstractmethod
    def set_scaling_mode(self, mode: str) -> None:
        """设置缩放模式: 'auto', 'quality', 'balanced', 'performance'"""
        pass


class IStatusIndicator(IUIComponent):
    """状态指示器接口"""

    @abstractmethod
    def set_active(self, active: bool) -> None:
        """设置激活状态"""
        pass

    @abstractmethod
    def is_active(self) -> bool:
        """获取当前状态"""
        pass


class IConnectionItem(IUIComponent):
    """连接状态项接口"""

    @abstractmethod
    def set_status(self, online: bool, text: Optional[str] = None) -> None:
        """设置连接状态"""
        pass

    @abstractmethod
    def get_name(self) -> str:
        """获取连接名称"""
        pass

    @abstractmethod
    def is_online(self) -> bool:
        """是否在线"""
        pass


class IGaugeBar(IUIComponent):
    """仪表进度条接口"""

    @abstractmethod
    def set_value(self, value: float, text: Optional[str] = None) -> None:
        """设置数值 (0.0 - max_value)"""
        pass

    @abstractmethod
    def get_value(self) -> float:
        """获取当前值"""
        pass

    @abstractmethod
    def set_max_value(self, max_val: float) -> None:
        """设置最大值"""
        pass

    @abstractmethod
    def set_color(self, color: str) -> None:
        """设置进度条颜色"""
        pass


class IDataCard(IUIComponent):
    """数据卡片接口"""

    @abstractmethod
    def set_title(self, title: str) -> None:
        """设置标题"""
        pass

    @abstractmethod
    def add_widget(self, widget) -> None:
        """添加子组件"""
        pass

    @abstractmethod
    def add_layout(self, layout) -> None:
        """添加子布局"""
        pass

    @abstractmethod
    def clear_content(self) -> None:
        """清空内容（保留标题）"""
        pass


class IStatItem(IUIComponent):
    """统计数据项接口"""

    @abstractmethod
    def set_value(self, value: str) -> None:
        """设置显示值"""
        pass

    @abstractmethod
    def get_value(self) -> str:
        """获取当前显示值"""
        pass

    @abstractmethod
    def set_label(self, label: str) -> None:
        """设置标签"""
        pass

    @abstractmethod
    def set_unit(self, unit: str) -> None:
        """设置单位"""
        pass


class IControlPanel(IUIComponent):
    """控制面板接口（右侧面板）"""

    @abstractmethod
    def get_connection_items(self) -> dict[str, IConnectionItem]:
        """获取所有连接状态项"""
        pass

    @abstractmethod
    def get_stat_items(self) -> dict[str, IStatItem]:
        """获取所有统计项"""
        pass

    @abstractmethod
    def set_reconnect_callback(self, callback) -> None:
        """设置重连按钮回调"""
        pass


class IVideoPanel(IUIComponent):
    """视频面板接口（左侧面板）"""

    @abstractmethod
    def get_video_display(self) -> IVideoDisplay:
        """获取视频显示组件"""
        pass

    @abstractmethod
    def show_overlay(self, text: str) -> None:
        """显示覆盖层消息"""
        pass

    @abstractmethod
    def hide_overlay(self) -> None:
        """隐藏覆盖层"""
        pass


class INavbar(IUIComponent):
    """导航栏接口"""

    @abstractmethod
    def set_title(self, title: str, subtitle: Optional[str] = None) -> None:
        """设置标题和副标题"""
        pass

    @abstractmethod
    def set_room(self, room: str) -> None:
        """设置房间号"""
        pass

    @abstractmethod
    def add_action(self, name: str, callback) -> None:
        """添加操作按钮"""
        pass

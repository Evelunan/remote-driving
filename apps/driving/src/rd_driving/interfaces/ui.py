"""
UI 组件接口定义

定义远程驾驶系统 UI 层的所有抽象接口，
实现面向接口编程，提高代码可扩展性和可测试性。
"""

from abc import ABC, abstractmethod
from typing import Optional


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

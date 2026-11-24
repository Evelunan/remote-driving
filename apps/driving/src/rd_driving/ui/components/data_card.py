"""
数据卡片组件 (PyQt6 优化版)

卡片容器，用于分组显示内容。支持动态内容管理、主题切换及默认样式兜底。
"""

from typing import List, Optional, Union

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QVBoxLayout, QWidget
from rd_driving.interfaces.ui import ITheme

# 默认样式常量 (深色模式风格，作为主题加载前的兜底)
DEFAULT_STYLES = {
    "bg_color": "#161B22",       # 类似 GitHub Dark Dimmed 背景
    "border_color": "#30363D",   # 边框颜色
    "title_color": "#ECEFF4",    # 标题颜色
    "radius": 6,
    "border_width": 1,
    "font_size": 15,
    "font_weight": 600,
    "margin": 16,
    "spacing": 12
}

class DataCard(QFrame):
    """数据卡片"""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._title_text = title
        self._theme: Optional[ITheme] = None
        self._content_widgets: List[QWidget] = []
        
        # 配置基础属性
        self.setObjectName("DataCard")
        # 设置默认样式，确保在未应用主题时也能正常显示
        self._apply_default_style()
        
        self._setup_ui()

    def _setup_ui(self):
        # 主布局
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(
            DEFAULT_STYLES["margin"], 
            DEFAULT_STYLES["margin"], 
            DEFAULT_STYLES["margin"], 
            DEFAULT_STYLES["margin"]
        )
        self._layout.setSpacing(DEFAULT_STYLES["spacing"])

        # 标题标签
        self._title_label = QLabel(self._title_text)
        self._title_label.setStyleSheet(f"""
            color: {DEFAULT_STYLES['title_color']};
            font-size: {DEFAULT_STYLES['font_size']}px;
            font-weight: {DEFAULT_STYLES['font_weight']};
        """)
        self._header_layout = QHBoxLayout()
        self._header_layout.setSpacing(8)
        self._header_layout.addWidget(self._title_label, 1)
        self._layout.addLayout(self._header_layout)

    def _apply_default_style(self):
        """应用默认兜底样式"""
        self.setStyleSheet(f"""
            #DataCard {{
                background-color: {DEFAULT_STYLES['bg_color']};
                border: {DEFAULT_STYLES['border_width']}px solid {DEFAULT_STYLES['border_color']};
                border-radius: {DEFAULT_STYLES['radius']}px;
            }}
        """)

    def apply_theme(self, theme: ITheme) -> None:
        """应用自定义主题"""
        self._theme = theme

        try:
            bg_color = theme.bg_card
            border_color = theme.border
            title_color = theme.text_primary
            
            # 处理字体家族，防止 None 或空字符串导致样式失效
            font_family = theme.get_font_family()
            font_rule = f"font-family: {font_family};" if font_family else ""
            
            # 应用卡片容器样式
            self.setStyleSheet(f"""
                #DataCard {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: {DEFAULT_STYLES['radius']}px;
                }}
            """)

            # 应用标题样式
            self._title_label.setStyleSheet(f"""
                color: {title_color};
                font-size: {DEFAULT_STYLES['font_size']}px;
                font-weight: {DEFAULT_STYLES['font_weight']};
                {font_rule}
            """)

            # 递归应用主题到已添加的子组件
            for widget in self._content_widgets:
                if hasattr(widget, 'apply_theme') and callable(widget.apply_theme):
                    widget.apply_theme(theme)
                    
        except AttributeError as e:
            # 如果主题对象缺少必要属性，记录错误并回退到默认样式
            print(f"Warning: Theme application failed due to missing attribute: {e}. Reverting to default style.")
            self._apply_default_style()

    def set_title(self, title: str) -> None:
        """设置或更新卡片标题"""
        self._title_text = title
        self._title_label.setText(title)

    def add_header_widget(self, widget: QWidget) -> None:
        self._header_layout.addWidget(widget)

    def add_widget(self, widget: QWidget) -> None:
        """
        向卡片添加子组件。
        如果已应用主题，会自动将主题传递给新组件。
        """
        self._layout.addWidget(widget)
        self._content_widgets.append(widget)

        if self._theme and hasattr(widget, 'apply_theme') and callable(widget.apply_theme):
            widget.apply_theme(self._theme)

    def add_layout(self, layout: QLayout) -> None:
        """
        向卡片添加子布局。
        注意：布局本身不接收主题，需确保布局内的 widget 已处理主题。
        """
        self._layout.addLayout(layout)
        # 布局不计入 _content_widgets 列表，因为无法直接对 QLayout 调用 apply_theme
        # 如果需要追踪布局内的 widget，需遍历布局

    def clear_content(self) -> None:
        """
        清空卡片内容区域（保留标题）。
        安全删除所有 widget 和嵌套布局。
        """
        # 从索引 1 开始，因为索引 0 是标题
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            self._dispose_layout_item(item)
        
        self._content_widgets.clear()

    def _dispose_layout_item(self, item: Union[QLayout, QWidget, None]):
        """递归销毁布局项中的 widget"""
        if item is None:
            return
            
        if item.widget():
            widget = item.widget()
            widget.deleteLater()
        elif item.layout():
            layout = item.layout()
            while layout.count():
                sub_item = layout.takeAt(0)
                self._dispose_layout_item(sub_item)
            # 布局本身不需要 deleteLater，但它包含的 widget 需要
            # 如果 layout 是动态创建的且没有父对象，可能需要考虑删除 layout 本身
            # 但在本例中，layout 是添加到主布局中的，主布局会管理它

    def set_margins(self, margin: int) -> None:
        """动态设置卡片内边距"""
        self._layout.setContentsMargins(margin, margin, margin, margin)

    def set_spacing(self, spacing: int) -> None:
        """动态设置卡片内组件间距"""
        self._layout.setSpacing(spacing)

    def get_widget(self) -> QWidget:
        return self

    def get_content_widgets(self) -> List[QWidget]:
        """获取当前内容组件列表的副本"""
        return list(self._content_widgets)

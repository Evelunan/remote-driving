"""
连接状态项组件 (PyQt6 优化版)

显示单个连接的状态信息，支持悬停高亮、动态状态文本及主题切换。
"""

from html import escape
from textwrap import wrap

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)
from rd_driving.interfaces.ui import ITheme

from .status_indicator import StatusIndicator

# 默认样式常量 (作为主题加载前的兜底)
DEFAULT_COLORS = {
    "name": "#8B949E",
    "status_online": "#3FB950",
    "status_offline": "#6E7681",
    "hover_bg": "rgba(255, 255, 255, 0.03)",  # 极淡的白色高光，适合深色模式
}

FONT_SIZES = {
    "name": 13,
    "status": 12,
}


class ConnectionItem(QFrame):
    """连接状态项"""

    def __init__(self, name: str):
        super().__init__()
        self._name_text = name
        self._online = False
        self._theme: ITheme | None = None
        self._action_button = None

        # 配置 QFrame 基础属性
        self.setObjectName("ConnectionItem")
        self.setStyleSheet("QFrame#ConnectionItem { background: transparent; }")
        # 确保启用鼠标事件以支持悬停效果
        self.setMouseTracking(True)

        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)  # 增加左右边距，使点击区域更舒适
        layout.setSpacing(6)

        # 1. 状态指示器 (小圆点)
        self._indicator = StatusIndicator(8)
        layout.addWidget(self._indicator)

        # 2. 名称标签
        self._name_label = QLabel(self._name_text)
        self._name_label.setStyleSheet(
            f"color: {DEFAULT_COLORS['name']}; font-size: {FONT_SIZES['name']}px;"
        )
        # Reserve identical name / status / action columns in every row.
        self._name_label.setFixedWidth(66)
        layout.addWidget(self._name_label)

        # 3. 状态文字 (在线/离线)
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._status_label.setMinimumWidth(40)
        self._status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # 初始样式
        self._update_status_style()
        layout.addWidget(self._status_label, 1)

        self._action_slot = QWidget(self)
        self._action_slot.setFixedSize(44, 28)
        action_layout = QHBoxLayout(self._action_slot)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(0)
        layout.addWidget(self._action_slot)

    def add_action(self, text, callback, tooltip):
        button = self._action_button = QPushButton(text)
        button.setFixedSize(44, 28)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        lines = [line for part in tooltip.split("；") for line in wrap(part, width=18)]
        button.setToolTip(
            "<qt>" + "<br>".join(f"<nobr>{escape(line)}</nobr>" for line in lines) + "</qt>"
        )
        button.setAccessibleDescription(tooltip)
        button.setAccessibleName(f"{self._name_text}：{text}")
        button.clicked.connect(callback)
        self._action_slot.layout().addWidget(button)
        return button

    def _get_status_text(self, online: bool, custom_text: str | None) -> str:
        """获取状态显示文本"""
        if custom_text:
            return custom_text
        return "在线" if online else "离线"

    def _update_status_style(self):
        """根据当前状态和主题更新状态标签样式"""
        if self._theme:
            try:
                color = self._theme.online if self._online else self._theme.text_muted
            except AttributeError:
                # 主题属性缺失时回退到默认颜色
                color = (
                    DEFAULT_COLORS["status_online"]
                    if self._online
                    else DEFAULT_COLORS["status_offline"]
                )
        else:
            color = (
                DEFAULT_COLORS["status_online"]
                if self._online
                else DEFAULT_COLORS["status_offline"]
            )

        weight = "bold" if self._online else "normal"

        # 仅更新必要的样式属性
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: {FONT_SIZES['status']}px; font-weight: {weight};"
        )

    def apply_theme(self, theme: ITheme) -> None:
        """应用主题"""
        self._theme = theme

        # 应用主题到子组件
        if hasattr(self._indicator, "apply_theme"):
            self._indicator.apply_theme(theme)

        # 更新名称颜色
        try:
            name_color = theme.text_secondary
        except AttributeError:
            name_color = DEFAULT_COLORS["name"]

        self._name_label.setStyleSheet(
            f"color: {name_color}; font-size: {FONT_SIZES['name']}px;"
        )

        # 刷新状态颜色
        self._update_status_style()

        if self._action_button is not None:
            self._action_button.setStyleSheet(
                theme.get_button_style()
                + f"""
                QPushButton {{ padding: 0px; border-radius: 8px; font-size: 12px; }}
                QToolTip {{
                    background-color: {theme.bg_secondary};
                    color: {theme.text_primary};
                    border: 1px solid {theme.border};
                    border-radius: 4px;
                    padding: 8px;
                    font-family: {theme.get_font_family()};
                    font-size: 12px;
                    font-weight: normal;
                    opacity: 255;
                }}
                """
            )

    def set_status(self, online: bool, text: str | None = None) -> None:
        """
        设置连接状态。

        Args:
            online: True 为在线，False 为离线。
            text: 可选的自定义状态文本。如果为 None，则自动显示 "在线" 或 "离线"。
        """
        if (
            self._online == online
            and self._status_label.text() == self._get_status_text(online, text)
        ):
            # 状态未变化且文本未变化，跳过更新以提升性能
            return

        self._online = online
        self._indicator.set_active(online)

        display_text = self._get_status_text(online, text)
        self._status_label.setText(display_text)

        self._update_status_style()

    def get_name(self) -> str:
        return self._name_text

    def is_online(self) -> bool:
        return self._online

    def get_widget(self) -> QWidget:
        return self

    # --- 交互效果优化 ---

    def enterEvent(self, event: QEvent) -> None:
        """鼠标进入时的高亮效果"""
        if self._theme and hasattr(self._theme, "bg_hover"):
            # 如果主题定义了悬停背景色，优先使用
            bg_color = self._theme.bg_hover
        else:
            # 默认悬停色 (半透明白/灰，适配深色模式)
            bg_color = DEFAULT_COLORS["hover_bg"]

        self.setStyleSheet(
            f"QFrame#ConnectionItem {{ background-color: {bg_color}; border-radius: 6px; }}"
        )
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """鼠标离开时恢复透明"""
        self.setStyleSheet("QFrame#ConnectionItem { background: transparent; }")
        super().leaveEvent(event)

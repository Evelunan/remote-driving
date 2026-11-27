"""
连接状态项组件 (PyQt6 优化版)

显示单个连接的状态信息，支持悬停高亮、动态状态文本及主题切换。
"""

from html import escape
from textwrap import wrap

from PyQt6.QtCore import Qt
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
    "name": "#CAD8E2",
    "status_online": "#82D6AA",
    "status_offline": "#AABBC8",
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
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(8)

        # 1. 状态指示器 (小圆点)
        self._indicator = StatusIndicator(8)
        layout.addWidget(self._indicator)

        # 2. 名称标签
        self._name_label = QLabel(self._name_text)
        self._name_label.setStyleSheet(
            f"color: {DEFAULT_COLORS['name']}; font-size: {FONT_SIZES['name']}px;"
        )
        # Keep names aligned while allowing read-only rows to use the full width.
        self._name_label.setFixedWidth(66)
        layout.addWidget(self._name_label)

        # 3. 状态文字 (在线/离线)
        self._status_label = QLabel(self._get_status_text(False, None))
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
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

    def add_action(self, text, callback, tooltip):
        button = self._action_button = QPushButton(text)
        button.setFixedSize(64, 28)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        lines = [line for part in tooltip.split("；") for line in wrap(part, width=18)]
        button.setToolTip(
            "<qt>" + "<br>".join(f"<nobr>{escape(line)}</nobr>" for line in lines) + "</qt>"
        )
        button.setAccessibleDescription(tooltip)
        self.set_action_text(text)
        button.clicked.connect(callback)
        self.layout().addWidget(button)
        return button

    def set_action_text(self, text):
        self._action_button.setText(text)
        self._action_button.setAccessibleName(f"{self._name_text}：{text}")

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
                f"""
                QPushButton {{
                    background: transparent;
                    color: {theme.text_primary};
                    border: 1px solid {theme.border};
                    border-radius: 4px;
                    padding: 0;
                    font-size: 12px;
                }}
                QPushButton:hover, QPushButton:pressed {{
                    background: {theme.bg_tertiary};
                }}
                QPushButton:focus {{ border-color: {theme.primary}; }}
                QPushButton:disabled {{
                    color: {theme.text_muted}; border-color: {theme.bg_tertiary};
                }}
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

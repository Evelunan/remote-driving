"""
控制面板组件

右侧控制面板，包含连接状态、车辆状态和实时数据
"""

from typing import Optional, Dict, Callable
from PyQt6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QScrollArea,
    QWidget,
    QPushButton,
    QGridLayout,
    QLabel,
    QSizePolicy,
)
from PyQt6.QtCore import Qt

from interfaces.ui import IConnectionItem, IStatItem, ITheme
from ..components import ConnectionItem, DataCard, StatItem


class ControlPanel(QFrame):
    """控制面板 - 使用 QFrame 包装 QScrollArea 确保高度一致"""

    def __init__(self):
        super().__init__()
        self._theme: Optional[ITheme] = None
        self._reconnect_callback: Optional[Callable] = None

        self.setFixedWidth(300)
        # 设置尺寸策略，让高度跟随父容器
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self._setup_ui()

    def _setup_ui(self):
        # 外层布局（加内边距）
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(8, 8, 8, 8)
        outer_layout.setSpacing(0)

        # 内部滚动区域
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )
        self._scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        # 主面板
        self._panel = QWidget()
        self._panel.setStyleSheet("background: transparent;")

        self._layout = QVBoxLayout(self._panel)
        self._layout.setContentsMargins(0, 0, 4, 0)
        self._layout.setSpacing(16)

        # === 连接状态卡片 ===
        self._conn_card = DataCard("系统连接")

        self._connections: Dict[str, ConnectionItem] = {
            "signal": ConnectionItem("信令服务器"),
            "webrtc": ConnectionItem("WebRTC"),
            "video": ConnectionItem("视频流"),
            "usb": ConnectionItem("控制设备"),
            "quic": ConnectionItem("数据通道"),
        }

        for conn in self._connections.values():
            self._conn_card.add_widget(conn)

        # 重连按钮
        self._reconnect_btn = QPushButton("重新连接")
        self._reconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reconnect_btn.setFixedHeight(38)
        self._reconnect_btn.clicked.connect(self._on_reconnect)
        self._conn_card.add_widget(self._reconnect_btn)

        self._layout.addWidget(self._conn_card)

        # === 车辆状态卡片 ===
        self._vehicle_card = DataCard("车辆状态")

        vehicle_grid = QGridLayout()
        vehicle_grid.setSpacing(12)

        self._vehicle_stats: Dict[str, StatItem] = {
            "speed": StatItem("车速", "km/h"),
            "battery": StatItem("电量", "%"),
            "gear": StatItem("档位", ""),
            "signal": StatItem("信号", ""),
        }

        vehicle_grid.addWidget(self._vehicle_stats["speed"], 0, 0)
        vehicle_grid.addWidget(self._vehicle_stats["battery"], 0, 1)
        vehicle_grid.addWidget(self._vehicle_stats["gear"], 1, 0)
        vehicle_grid.addWidget(self._vehicle_stats["signal"], 1, 1)

        self._vehicle_card.add_layout(vehicle_grid)

        # 占位提示
        self._vehicle_placeholder = QLabel("等待服务器数据...")
        self._vehicle_placeholder.setStyleSheet(
            "color: #6E7681; font-size: 11px; padding-top: 8px; border: none;"
        )
        self._vehicle_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vehicle_card.add_widget(self._vehicle_placeholder)

        self._layout.addWidget(self._vehicle_card)

        # 弹性空间填充剩余高度
        self._layout.addStretch(1)

        self._scroll_area.setWidget(self._panel)
        outer_layout.addWidget(self._scroll_area)

    def get_widget(self):
        return self

    def apply_theme(self, theme: ITheme) -> None:
        self._theme = theme

        # 外层 Frame 设置背景和圆角，与 VideoPanel 一致
        self.setObjectName("controlPanel")
        self.setStyleSheet(
            f"""
            #controlPanel {{
                background-color: {theme.bg_secondary};
                border: 1px solid {theme.border};
                border-radius: 12px;
            }}
        """
        )

        # 滚动条样式
        self._scroll_area.setStyleSheet(
            f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {theme.get_scrollbar_style()}
        """
        )

        # 卡片样式
        self._conn_card.apply_theme(theme)
        self._vehicle_card.apply_theme(theme)

        # 连接项样式
        for conn in self._connections.values():
            conn.apply_theme(theme)

        # 统计项样式
        for stat in self._vehicle_stats.values():
            stat.apply_theme(theme)

        # 按钮样式
        self._reconnect_btn.setStyleSheet(theme.get_button_style())
        self._reconnect_btn.setFixedHeight(38)

        # 占位文字
        self._vehicle_placeholder.setStyleSheet(
            f"color: {theme.text_muted}; font-size: 11px; padding-top: 8px; border: none;"
        )

    def get_connection_items(self) -> Dict[str, IConnectionItem]:
        return self._connections

    def get_stat_items(self) -> Dict[str, IStatItem]:
        return { **self._vehicle_stats}

    def set_reconnect_callback(self, callback: Callable) -> None:
        self._reconnect_callback = callback

    def _on_reconnect(self):
        if self._reconnect_callback:
            self._reconnect_callback()

    # === 便捷方法 ===

    def set_connection_status(
        self, name: str, online: bool, text: Optional[str] = None
    ):
        """设置连接状态"""
        if name in self._connections:
            self._connections[name].set_status(online, text)

    def set_stat_value(self, name: str, value: str):
        """设置统计值"""
        if name in self._vehicle_stats:
            self._vehicle_stats[name].set_value(value)

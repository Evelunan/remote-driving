"""
控制面板组件

右侧控制面板，包含连接状态、车辆状态和实时数据
"""

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from rd_driving.interfaces.ui import IConnectionItem, ITheme

from ..components import ConnectionItem, DataCard, StatItem


class ControlPanel(QFrame):
    """控制面板 - 使用 QFrame 包装 QScrollArea 确保高度一致"""

    def __init__(self):
        super().__init__()
        self._theme: ITheme | None = None
        self._media_reconnect_callback: Callable | None = None
        self._control_reconnect_callback: Callable | None = None
        self._usb_connect_callback: Callable | None = None

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

        self._connections: dict[str, ConnectionItem] = {
            "signal": ConnectionItem("信令服务器"),
            "webrtc": ConnectionItem("WebRTC"),
            "video": ConnectionItem("视频流"),
            "usb": ConnectionItem("控制设备"),
            "quic": ConnectionItem("数据通道"),
        }

        for conn in self._connections.values():
            self._conn_card.add_widget(conn)

        media_hint = "重建信令与视频连接；画面恢复前暂停控制"
        self._action_buttons = {
            name: self._connections[name].add_action(
                "重连", self._on_media_reconnect, media_hint
            )
            for name in ("signal", "webrtc")
        }
        self._usb_connect_btn = self._connections["usb"].add_action(
            "连接", self._on_usb_connect, "探测并连接控制设备"
        )
        self._action_buttons["usb"] = self._usb_connect_btn
        self._usb_connect_btn.setEnabled(False)
        self._action_buttons["quic"] = self._connections["quic"].add_action(
            "重连",
            self._on_control_reconnect,
            "重连数据通道并申请控制权；保留设备和视频连接",
        )

        self._layout.addWidget(self._conn_card)

        self._command_card = DataCard("底盘接口执行确认")
        self._command_stats: dict[str, StatItem] = {
            "throttle": StatItem("油门目标", "%"),
            "steering": StatItem("转向目标", "%"),
            "brake": StatItem("制动输入", "%"),
            "elapsed": StatItem("采样至确认", "ms"),
        }

        command_grid = QGridLayout()
        command_grid.setSpacing(16)

        command_grid.addWidget(self._command_stats["throttle"], 0, 0)
        command_grid.addWidget(self._command_stats["steering"], 0, 1)
        command_grid.addWidget(self._command_stats["brake"], 1, 0)
        command_grid.addWidget(self._command_stats["elapsed"], 1, 1)

        self._command_card.add_layout(command_grid)
        self._measurement_note = QLabel("实测车速和停车反馈：尚未接入")
        self._measurement_note.setWordWrap(True)
        self._command_card.add_widget(self._measurement_note)
        self.clear_command_status()
        self._layout.addWidget(self._command_card)

        self.CAMERA_NAMES = ("front", "back", "left", "right")

        self.camera_cards: dict[str, DataCard] = {}
        self.camera_data: dict[str, dict[str, StatItem]] = {}
        for cam in self.CAMERA_NAMES:
            card, data = self._add_camera_card(cam)
            self.camera_cards[cam] = card
            self.camera_data[cam] = data
            self._layout.addWidget(card)

        # 弹性空间填充剩余高度
        self._layout.addStretch(1)

        self._scroll_area.setWidget(self._panel)
        outer_layout.addWidget(self._scroll_area)

    def _add_camera_card(self, key):
        camera_card = DataCard(key)
        grid = QGridLayout()
        grid.setSpacing(16)

        data: dict[str, StatItem] = {
            "fps": StatItem("帧率", "FPS"),
            "resolution": StatItem("分辨率", ""),
            "latency": StatItem("画面延迟", "ms"),
        }
        grid.addWidget(data["fps"], 0, 0)
        grid.addWidget(data["resolution"], 0, 1)
        grid.addWidget(data["latency"], 1, 0, 1, 2)
        data["latency"].setToolTip("当前已绘制帧距车端采集的估算时间，两端通过 WebRTC 校时。")
        camera_card.add_layout(grid)

        return camera_card, data

    def set_measured_status(self, linear_mps, angular_radps):
        self._measurement_note.setText(
            f"实测线速度：{linear_mps:.2f} m/s\n实测角速度：{angular_radps:.2f} rad/s\n停车反馈：尚未接入"
        )

    def clear_measured_status(self):
        self._measurement_note.setText("实测车速：未接入或已过期；停车反馈：尚未接入")

    def get_widget(self):
        return self

    def apply_theme(self, theme: ITheme) -> None:
        self._theme = theme

        # 外层 Frame 设置背景和圆角
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

        # 连接项样式
        for conn in self._connections.values():
            conn.apply_theme(theme)

        self._command_card.apply_theme(theme)
        for item in self._command_stats.values():
            item.apply_theme(theme)

        for card in self.camera_cards.values():
            card.apply_theme(theme)

        for data in self.camera_data.values():
            for item in data.values():
                item.apply_theme(theme)

    def get_connection_items(self) -> dict[str, IConnectionItem]:
        return self._connections

    def set_media_reconnect_callback(self, callback: Callable) -> None:
        self._media_reconnect_callback = callback

    def set_media_reconnecting(self, busy):
        for name in ("signal", "webrtc"):
            self._action_buttons[name].setEnabled(not busy)

    def _on_media_reconnect(self):
        if self._media_reconnect_callback:
            self.set_media_reconnecting(True)
            if not self._media_reconnect_callback():
                self.set_media_reconnecting(False)

    def set_control_reconnect_callback(self, callback: Callable) -> None:
        self._control_reconnect_callback = callback

    def _on_control_reconnect(self):
        if self._control_reconnect_callback:
            self._action_buttons["quic"].setEnabled(False)
            if not self._control_reconnect_callback():
                self._action_buttons["quic"].setEnabled(True)

    def set_usb_connect_callback(self, callback: Callable) -> None:
        self._usb_connect_callback = callback

    def _on_usb_connect(self):
        if self._usb_connect_callback:
            self._usb_connect_btn.setEnabled(False)
            if not self._usb_connect_callback():
                self._usb_connect_btn.setEnabled(True)

    # === 便捷方法 ===

    def set_connection_status(self, name: str, online: bool, text: str | None = None):
        """设置连接状态"""
        if name in self._connections:
            self._connections[name].set_status(online, text)
        if name == "usb":
            self._usb_connect_btn.setEnabled(not online and text == "未连接")
        elif name == "quic":
            self._action_buttons["quic"].setEnabled(text != "连接中")

    def set_command_status(self, throttle, brake, steering, elapsed_ms):
        for key, value in zip(
            ("throttle", "brake", "steering", "elapsed"),
            (throttle, brake, steering, elapsed_ms),
        ):
            self._command_stats[key].set_value(value)

    def clear_command_status(self):
        for item in self._command_stats.values():
            item.set_value("--")

    def set_camera_grid(self, key, fps, w, h, latency_ms=None):
        data = self.camera_data[key]
        data["fps"].set_value(fps)
        data["resolution"].set_value(f"{w}×{h}")
        data["latency"].set_value(latency_ms)

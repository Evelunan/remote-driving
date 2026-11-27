"""驾驶工作台的输入、反馈和连接侧栏。"""

from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from rd_core.wire import VehicleStatus
from rd_driving.interfaces.ui import IConnectionItem, ITheme

from ..components import ConnectionItem, DataCard, StatItem
from .multi_video_widget import CAMERA_NAMES, DEFAULT_LABELS


class ControlPanel(QFrame):
    """将关键状态置于上方，将低频查看的统计与诊断放入折叠区域。"""

    video_toggle_requested = pyqtSignal()
    control_toggle_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._media_reconnect_callback: Callable | None = None
        self._control_reconnect_callback: Callable | None = None
        self._usb_connect_callback: Callable | None = None
        self._video_requested = False

        self.setFixedWidth(328)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setObjectName("controlPanel")
        self.setAccessibleName("驾驶数据与连接")
        self._setup_ui()

    def _setup_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._panel = QWidget()
        self._panel.setObjectName("controlPanelContent")
        self._layout = QVBoxLayout(self._panel)
        self._layout.setContentsMargins(0, 0, 3, 0)
        self._layout.setSpacing(5)

        self._setup_vehicle_status()
        self._setup_local_input()
        self._setup_confirmation()
        self._setup_connections()
        self._setup_diagnostics()
        self._layout.addStretch()
        self._scroll_area.setWidget(self._panel)
        outer_layout.addWidget(self._scroll_area)

    @staticmethod
    def _section(title):
        card = DataCard(title)
        card.set_margins(6)
        card.set_spacing(4)
        return card

    @staticmethod
    def _disclosure(label, content):
        button = QPushButton(f"展开{label}")
        button.setCheckable(True)
        button.setMinimumHeight(30)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        content.hide()

        def set_expanded(expanded):
            content.setVisible(expanded)
            button.setText(f"{'收起' if expanded else '展开'}{label}")

        button.toggled.connect(set_expanded)
        return button

    def _setup_connections(self):
        self._conn_card = self._section("系统连接")
        self._conn_card.set_spacing(2)
        self._connections: dict[str, ConnectionItem] = {
            "usb": ConnectionItem("控制设备"),
            "quic": ConnectionItem("数据通道"),
            "video": ConnectionItem("视频流"),
            "control": ConnectionItem("行驶授权"),
            "webrtc": ConnectionItem("WebRTC"),
            "signal": ConnectionItem("信令服务器"),
        }

        for conn in self._connections.values():
            self._conn_card.add_widget(conn)

        media_hint = "重建信令与视频连接"
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
            "重连车辆状态的数据通道；保留设备和视频连接",
        )
        self._action_buttons["video"] = self._connections["video"].add_action(
            "连接视频", self.video_toggle_requested.emit, "开启或关闭车辆视频",
        )
        self._action_buttons["control"] = self._connections["control"].add_action(
            "开始控制", self.control_toggle_requested.emit, "开启、恢复或停止行驶控制",
        )
        self.set_connection_status("video", False, "未连接")
        self.set_connection_status("control", False, "未开启")
        self.set_media_reconnecting(False)
        self.set_control_action("开始控制", enabled=False)

        self._layout.addWidget(self._conn_card)

    def _setup_diagnostics(self):
        self._setup_vehicle_diagnostics()
        self._setup_camera_statistics()

        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(self._vehicle_details_button, 1)
        actions.addWidget(self._camera_details_button, 1)
        self._layout.addLayout(actions)
        self._layout.addWidget(self._vehicle_details)
        self._layout.addWidget(self._camera_details)

    def _setup_camera_statistics(self):
        self.CAMERA_NAMES = CAMERA_NAMES
        self._camera_details = QWidget()
        details = QVBoxLayout(self._camera_details)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(6)
        self._camera_details_button = self._disclosure("视频统计", self._camera_details)
        self.camera_cards: dict[str, DataCard] = {}
        self.camera_data: dict[str, dict[str, StatItem]] = {}
        for cam in self.CAMERA_NAMES:
            card, data = self._add_camera_card(cam)
            self.camera_cards[cam] = card
            self.camera_data[cam] = data
            details.addWidget(card)

    def _setup_local_input(self):
        self._input_card = self._section("驾驶端输入")
        self._input_note = QLabel("输入：等待采样")
        self._input_card.add_header_widget(self._input_note)
        self._gear = StatItem("所选挡位")
        self._gear.setToolTip("所选挡位决定后续行驶方向。")
        self._input_stats = {
            "throttle": StatItem("油门输入", "%"),
            "brake": StatItem("制动输入", "%"),
            "steering": StatItem("转向输入", "%"),
        }
        self._input_stats["brake"].setToolTip("制动输入转换为零速请求。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        metrics = (
            self._gear,
            self._input_stats["steering"],
            self._input_stats["throttle"],
            self._input_stats["brake"],
        )
        for index, widget in enumerate(metrics):
            grid.addWidget(widget, index // 2, index % 2)
        self._input_card.add_layout(grid)
        self._layout.addWidget(self._input_card)

    def _setup_vehicle_status(self):
        self._vehicle_card = self._section("车辆反馈")
        self._vehicle_note = QLabel()
        self._vehicle_card.add_header_widget(self._vehicle_note)
        self._vehicle_stats = {
            "speed_mps": StatItem("实际车速", "m/s"),
            "steering_deg": StatItem("转向角", "°"),
            "shift_level": StatItem("反馈挡位"),
            "is_autodrive": StatItem("自动驾驶使能"),
            "p_status": StatItem("底盘制动反馈"),
            "left_wheel_mps": StatItem("左轮速度", "m/s"),
            "right_wheel_mps": StatItem("右轮速度", "m/s"),
            "drive_mode": StatItem("驾驶模式（原始编码）"),
        }
        self._vehicle_stats["shift_level"].setToolTip(
            "1 为 D，3 为 R；2 在源码中为 N、底盘说明中为停止，不构成车辆已停稳的证明。"
        )
        self._vehicle_stats["p_status"].setToolTip(
            "底盘返回的 p_status；与驾驶端的制动输入分别显示。"
        )
        for keys in (
            ("speed_mps", "shift_level", "steering_deg"),
            ("p_status", "is_autodrive"),
        ):
            row = QHBoxLayout()
            row.setSpacing(10)
            for key in keys:
                row.addWidget(self._vehicle_stats[key], 1, Qt.AlignmentFlag.AlignTop)
            self._vehicle_card.add_layout(row)
        self.clear_vehicle_status()
        self._layout.addWidget(self._vehicle_card)

    def _setup_confirmation(self):
        self._confirmation_card = self._section("指令确认")
        self._confirmation_state = StatItem("接口状态")
        self._command_stats = {
            "throttle": StatItem("已确认油门（含方向）", "%"),
            "brake": StatItem("已确认制动输入", "%"),
            "steering": StatItem("已确认转向输入", "%"),
            "elapsed": StatItem("采样至确认", "ms"),
        }
        confirmation = QGridLayout()
        confirmation.setHorizontalSpacing(10)
        confirmation.setColumnStretch(0, 1)
        confirmation.setColumnStretch(1, 1)
        confirmation.addWidget(self._confirmation_state, 0, 0)
        confirmation.addWidget(self._command_stats["elapsed"], 0, 1)
        self._confirmation_card.add_layout(confirmation)
        self._confirmation_note = QLabel("接口确认仅表示车端接口调用完成。")
        self._confirmation_note.setToolTip("底盘实际状态和停车确认各有独立反馈。")
        self._confirmation_note.setWordWrap(True)
        self._confirmation_card.add_widget(self._confirmation_note)
        self.clear_command_status()
        self._layout.addWidget(self._confirmation_card)

    def _setup_vehicle_diagnostics(self):
        self._vehicle_details = QWidget()
        self._vehicle_details_button = self._disclosure("诊断详情", self._vehicle_details)
        details = QGridLayout(self._vehicle_details)
        details.setContentsMargins(10, 6, 10, 6)
        details.setHorizontalSpacing(10)
        details.setVerticalSpacing(5)
        for index, key in enumerate(("left_wheel_mps", "right_wheel_mps", "drive_mode")):
            details.addWidget(self._vehicle_stats[key], index // 2, index % 2)
        for index, key in enumerate(("throttle", "brake", "steering")):
            details.addWidget(self._command_stats[key], 2 + index // 2, index % 2)
        self._measurement_note = QLabel("独立遥测：未接入；停车确认：未接入")
        self._measurement_note.setWordWrap(True)
        details.addWidget(self._measurement_note, 4, 0, 1, 2)

    def set_vehicle_status(self, status: VehicleStatus):
        for key, item in self._vehicle_stats.items():
            value = getattr(status, key)
            if value is not None:
                if key == "is_autodrive":
                    value = "已使能" if value else "未使能"
                elif key == "shift_level":
                    value = {1: "D 前进（1）", 2: "N／停止（2）", 3: "R 倒退（3）"}.get(
                        value, f"未知（{value}）"
                    )
                elif key == "p_status":
                    value = "已触发" if value else "未触发"
            item.set_value(value)
        partial = any(value is None for value in vars(status).values())
        self._vehicle_note.setText("反馈：实时，部分字段未知" if partial else "反馈：实时")

    def clear_vehicle_status(self, reason="反馈未接入"):
        for item in self._vehicle_stats.values():
            item.set_value(None)
        self._vehicle_note.setText(reason)

    def _add_camera_card(self, key):
        camera_card = self._section(DEFAULT_LABELS[key])
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)

        data: dict[str, StatItem] = {
            "fps": StatItem("帧率", "FPS"),
            "resolution": StatItem("分辨率", ""),
            "latency": StatItem("画面年龄", "ms"),
        }
        grid.addWidget(data["fps"], 0, 0)
        grid.addWidget(data["resolution"], 0, 1)
        grid.addWidget(data["latency"], 0, 2)
        data["latency"].setToolTip("当前已绘制帧距车端采集的估算时间，两端通过 WebRTC 校时。")
        camera_card.add_layout(grid)

        return camera_card, data

    def set_measured_status(self, linear_mps, angular_radps):
        self._measurement_note.setText(
            f"实测线速度：{linear_mps:.2f} m/s\n实测角速度：{angular_radps:.2f} rad/s\n停车反馈：尚未接入"
        )

    def clear_measured_status(self):
        self._measurement_note.setText("独立遥测：未接入或已过期；停车确认：尚未接入")

    def get_widget(self):
        return self

    def apply_theme(self, theme: ITheme) -> None:
        self.setStyleSheet(
            f"""
            #controlPanel, #controlPanelContent {{
                background-color: {theme.bg_primary};
            }}
            """
        )
        self._scroll_area.setStyleSheet(
            f"""
            QScrollArea {{ border: none; background-color: {theme.bg_primary}; }}
            {theme.get_scrollbar_style()}
            """
        )

        for card in (
            self._vehicle_card, self._input_card, self._confirmation_card, self._conn_card
        ):
            card.apply_theme(theme)
        self._gear.apply_theme(theme, prominent_label=True, value_size=19)
        for item in self._input_stats.values():
            item.apply_theme(theme, prominent_label=True, value_size=19)
        for key, item in self._vehicle_stats.items():
            if key == "speed_mps":
                item.apply_theme(theme, prominent_label=True, value_size=25)
            else:
                item.apply_theme(theme, value_size=16)
        for label in (
            self._input_note, self._vehicle_note, self._confirmation_note, self._measurement_note
        ):
            label.setStyleSheet(f"color: {theme.text_secondary}; font-size: 12px;")

        for button in (self._vehicle_details_button, self._camera_details_button):
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {theme.bg_card};
                    color: {theme.text_primary};
                    border: 1px solid {theme.border};
                    border-radius: 4px;
                    padding: 0 10px;
                    text-align: left;
                    font-size: 12px;
                }}
                QPushButton:hover, QPushButton:checked {{
                    background-color: {theme.bg_tertiary};
                }}
                QPushButton:focus {{ border-color: {theme.primary}; }}
                """
            )
        self._confirmation_state.apply_theme(theme, value_size=16)
        for item in self._command_stats.values():
            item.apply_theme(theme, value_size=16)

        for card in self.camera_cards.values():
            card.apply_theme(theme)

        for data in self.camera_data.values():
            for item in data.values():
                item.apply_theme(theme, value_size=15)

    def get_connection_items(self) -> dict[str, IConnectionItem]:
        return self._connections

    def set_media_reconnect_callback(self, callback: Callable) -> None:
        self._media_reconnect_callback = callback

    def set_video_requested(self, requested, *, busy=False):
        self._video_requested = requested
        self._connections["video"].set_action_text("断开视频" if requested else "连接视频")
        self._action_buttons["video"].setEnabled(not busy or requested)
        self.set_media_reconnecting(busy)

    def set_media_reconnecting(self, busy):
        for name in ("signal", "webrtc"):
            self._action_buttons[name].setEnabled(self._video_requested and not busy)

    def set_control_action(self, text, *, enabled=True):
        self._connections["control"].set_action_text(text)
        self._action_buttons["control"].setEnabled(enabled)

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

    def set_local_input(self, state, reason="输入：无效或已过期"):
        if state is None:
            self._gear.set_value(None)
            for item in self._input_stats.values():
                item.set_value(None)
            self._input_note.setText(reason)
        else:
            self._gear.set_value("R 倒挡" if state.reverse else "D 前进挡")
            self._input_stats["throttle"].set_value(abs(state.sample.throttle) * 100)
            self._input_stats["brake"].set_value(state.sample.brake * 100)
            self._input_stats["steering"].set_value(state.sample.steering * 100)
            self._input_note.setText("输入：有效")

    def set_connection_status(self, name: str, online: bool, text: str | None = None):
        """设置连接状态"""
        if name in self._connections:
            self._connections[name].set_status(online, text)
        if name == "usb":
            self._usb_connect_btn.setEnabled(not online and text == "未连接")
            if not online:
                self.set_local_input(None, "输入：设备未连接")
        elif name == "quic":
            self._action_buttons["quic"].setEnabled(text != "连接中")

    def set_command_status(self, throttle, brake, steering, elapsed_ms):
        self._confirmation_state.set_value("接口已确认")
        for key, value in zip(
            ("throttle", "brake", "steering", "elapsed"),
            (throttle, brake, steering, elapsed_ms),
        ):
            self._command_stats[key].set_value(value)

    def clear_command_status(self, reason="待确认"):
        self._confirmation_state.set_value(reason)
        for item in self._command_stats.values():
            item.set_value("--")

    def set_camera_grid(self, key, fps, w, h, latency_ms=None):
        data = self.camera_data[key]
        has_frame = w > 0 and h > 0
        data["fps"].set_value(fps if has_frame and fps else None)
        data["resolution"].set_value(f"{w}×{h}" if has_frame else None)
        data["latency"].set_value(latency_ms if has_frame else None)

"""
远程驾驶控制系统 - 主界面

模块化 UI，使用工厂模式创建组件。
"""

import sys
import time
from dataclasses import replace

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from rd_core.configuration import load_config
from rd_core.logging import get_logger
from rd_core.settings import DrivingSettings
from rd_core.video_safety import VideoGate

from rd_driving.branding import APP_NAME, APP_NAME_EN, VERSION
from rd_driving.factories import UIFactory
from rd_driving.infrastructure.video import FrameBuffer, WebRTCReceiver
from rd_driving.infrastructure.workers import (
    ControlSystemThread,
    SignalBridge,
    WebRTCReceiverThread,
)
from rd_driving.infrastructure.workers.control_system_thread import STATUS_TEXT
from rd_driving.shutdown import shutdown_signals
from rd_driving.ui.widgets import LoginPage
from rd_driving.ui.widgets.multi_video_widget import CAMERA_NAMES, MultiVideoWidget

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    """主窗口 - 包含登录页面和控制页面"""

    def __init__(
        self, session_id: int | None = None, room: str | None = None, *, settings=None
    ):
        super().__init__()
        self.settings = settings or load_config(DrivingSettings, "driving").value
        self.video_gate = VideoGate(
            stale_frame_ms=self.settings.video.stale_frame_ms,
            recovery_ms=self.settings.video.recovery_ms,
            max_capture_age_ms=self.settings.video.max_capture_age_ms,
        )
        self._session_id = session_id
        self._room = room
        self._theme = UIFactory.set_theme(self.settings.ui.theme)

        # 服务相关
        self._receiver_thread = None
        self._control_thread = None
        self._service_generation = 0
        self._media_generation = 0
        self._media_restart_pending = False
        self._pending_action = None
        self._closing_ready = False
        self._shutdown_poll = QTimer(self)
        self._shutdown_poll.setInterval(25)
        self._shutdown_poll.timeout.connect(self._finish_shutdown)

        # UI 组件引用（用于安全检查）
        self._control_panel = None
        self._navbar = None
        self._time_label = None

        # 标记服务是否正在运行
        self._services_running = False

        # WebRTC 连接状态标记（只有连接成功后才允许显示视频传输状态）
        self._webrtc_connected = False
        self._telemetry_at = None
        self._vehicle_status_deadline = None
        self._local_input_deadline = None
        self._command_deadline = None
        self._command_confirmed_once = False
        self.fb = FrameBuffer()

        # 登录数据
        self._login_data = {}
        if session_id is not None and room:
            self._login_data = {"session_id": session_id, "room": room}
        logger.info(self._login_data)
        self._setup_ui()

        # 如果有 session_id，直接进入主控制页面
        if session_id is not None and room:
            self._switch_to_control_page()

    def _apply_global_style(self):
        self.setStyleSheet(
            f"""
            QMainWindow {{
                background-color: {self._theme.bg_primary};
            }}
            QLabel {{
                color: {self._theme.text_primary};
                font-family: {self._theme.get_font_family()};
            }}
            {self._theme.get_scrollbar_style()}
        """
        )

    def _setup_signals(self):
        self._bridge = SignalBridge()
        generation = self._service_generation
        for signal, callback in (
            (self._bridge.usb_status_changed, self._on_usb_status_changed),
            (self._bridge.local_input_changed, self._on_local_input_changed),
            (self._bridge.quic_status_changed, self._on_quic_status_changed),
            (self._bridge.control_data_updated, self._on_control_data_updated),
            (
                self._bridge.control_confirmation_expired,
                self._on_control_confirmation_expired,
            ),
            (self._bridge.vehicle_status_updated, self._on_vehicle_status),
        ):
            signal.connect(
                lambda *args, g=generation, cb=callback: self._generation_signal(
                    g, cb, *args
                )
            )

    def _generation_signal(self, generation, callback, *args):
        if generation == self._service_generation and self._services_running:
            callback(*args)

    def _on_control_data_updated(self, data):
        if self._services_running and self._control_panel:
            if data["deadline"] <= time.monotonic():
                self._on_control_confirmation_expired()
                return
            self._command_deadline = data["deadline"]
            self._command_confirmed_once = True
            sample = data["sample"]
            self._control_panel.set_command_status(
                sample.throttle * 100, sample.brake * 100,
                sample.steering * 100, data["elapsed_ms"],
            )

    def _on_control_confirmation_expired(self):
        self._command_deadline = None
        if self._services_running and self._control_panel:
            self._control_panel.clear_command_status(
                "确认已过期" if self._command_confirmed_once else "待确认"
            )

    def _on_vehicle_status(self, data):
        if self._services_running and self._control_panel is not None:
            if "status" in data:
                if data["deadline"] <= time.monotonic():
                    self._vehicle_status_deadline = None
                    self._control_panel.clear_vehicle_status("反馈已过期")
                    return
                self._vehicle_status_deadline = data["deadline"]
                self._control_panel.set_vehicle_status(data["status"])
                return
            self._control_panel.set_measured_status(
                data["linear_mps"], data["angular_radps"]
            )
            self._telemetry_at = time.monotonic()

    def _expire_telemetry(self):
        now = time.monotonic()
        if self._local_input_deadline is not None and now >= self._local_input_deadline:
            self._on_local_input_changed(None)
        if self._command_deadline is not None and now >= self._command_deadline:
            self._on_control_confirmation_expired()
        if (self._vehicle_status_deadline is not None
                and time.monotonic() >= self._vehicle_status_deadline
                and self._control_panel is not None):
            self._control_panel.clear_vehicle_status("反馈已过期")
            self._vehicle_status_deadline = None
        if (
            self._telemetry_at is not None
            and self._control_panel is not None
            and time.monotonic() - self._telemetry_at
            >= self.settings.control.command_timeout_ms / 1000
        ):
            self._control_panel.clear_measured_status()
            self._telemetry_at = None

    def _on_usb_status_changed(self, ok: bool, text: str):
        if not ok:
            self._local_input_deadline = None
        if self._services_running and self._control_panel:
            self._control_panel.set_connection_status("usb", ok, text)

    def _on_local_input_changed(self, state):
        if self._control_panel is not None:
            timeout = self.settings.control.input_timeout_ms / 1000
            if state is None or not state.sample.fresh(timeout):
                state = None
            self._local_input_deadline = (
                state.sample.sampled_at + timeout if state is not None else None
            )
            self._control_panel.set_local_input(state)

    def _on_quic_status_changed(self, ok: bool, text: str):
        if self._services_running and self._control_panel:
            connection_text = STATUS_TEXT["registered"] if ok else text
            self._control_panel.set_connection_status("quic", ok, connection_text)
            if self._navbar is not None:
                tone = "waiting"
                if ok and text == STATUS_TEXT["active"]:
                    tone = "active"
                elif text == STATUS_TEXT["waiting_video"] or (
                    not ok and text != STATUS_TEXT["connecting"]
                ):
                    tone = "blocked"
                self._navbar.set_control_status(text, tone=tone)
            if not ok:
                self._clear_feedback()

    def _clear_feedback(self):
        self._telemetry_at = None
        self._vehicle_status_deadline = None
        self._command_deadline = None
        self._command_confirmed_once = False
        if self._control_panel is not None:
            self._control_panel.clear_command_status()
            self._control_panel.clear_measured_status()
            self._control_panel.clear_vehicle_status("反馈未接入或连接已断开")

    def _setup_ui(self):
        self.setWindowTitle(f"{APP_NAME} | {APP_NAME_EN}")
        self.setMinimumSize(self.settings.ui.width, self.settings.ui.height)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 使用 QStackedWidget 管理页面切换
        self._stacked_widget = QStackedWidget()

        # 页面 0: 登录页面
        self._login_page = LoginPage(self._theme, settings=self.settings)
        self._login_page.login_success.connect(self._on_login_success)
        self._stacked_widget.addWidget(self._login_page)

        # 页面 1: 控制页面（先创建空容器，登录后再填充）
        self._control_page = QWidget()
        self._stacked_widget.addWidget(self._control_page)

        main_layout.addWidget(self._stacked_widget)
        self._apply_global_style()

    def _on_login_success(self, data: dict):
        """登录成功，切换到控制页面"""
        logger.info(f"登录成功: {data}")
        self._login_data = data
        try:
            selected = replace(
                self.settings, session=int(data["session_id"]), room=data["room"]
            )
            selected.validate()
        except (KeyError, TypeError, ValueError):
            self._login_page._show_error("会话或房间参数无效")
            return
        self._session_id, self._room = selected.session, selected.room
        self._switch_to_control_page()

    def _switch_to_control_page(self):
        """切换到控制页面并启动服务"""
        self._setup_control_page()
        self._setup_timers()
        self._services_running = True
        self._start_services()
        self._stacked_widget.setCurrentIndex(1)

    def _setup_control_page(self):
        """设置控制页面的 UI"""
        layout = QVBoxLayout(self._control_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 导航栏
        self._navbar = UIFactory.create_navbar()

        self._navbar.set_room(self._room)
        self._navbar.set_session(self._session_id)
        self._navbar.add_action("退出会话", self._logout, style="secondary")
        layout.addWidget(self._navbar.get_widget())

        # 内容区 - 使用 QFrame 作为容器确保子组件等高
        content = QFrame()
        content.setStyleSheet("background: transparent; border: none;")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(16, 12, 16, 12)
        content_layout.setSpacing(12)

        self._multi_video_widget = MultiVideoWidget(
            self.fb,
            stale_frame_ms=self.settings.video.stale_frame_ms,
            debug_overlay=self.settings.ui.debug,
            video_gate=self.video_gate,
            primary_view=self.settings.video.primary_view,
        )
        self._multi_video_widget.apply_theme(self._theme)
        self._control_panel = UIFactory.create_control_panel()
        self._control_panel.set_media_reconnect_callback(self._reconnect_media)
        self._control_panel.set_control_reconnect_callback(self._reconnect_control)
        self._control_panel.set_usb_connect_callback(self._connect_usb)

        # 直接添加组件，不设置 alignment，让 QHBoxLayout 自动拉伸高度
        content_layout.addWidget(self._multi_video_widget, 1)
        content_layout.addWidget(self._control_panel, 0)
        layout.addWidget(content, 1)

        # 底部栏
        layout.addWidget(self._create_footer())

    def _create_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("appFooter")
        footer.setFixedHeight(28)
        footer.setStyleSheet(
            f"""
            #appFooter {{
                background-color: {self._theme.bg_secondary};
                border: none;
                border-top: 1px solid {self._theme.border};
            }}
        """
        )
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(16, 0, 16, 0)

        version_label = QLabel(f"v{VERSION}")
        version_label.setStyleSheet(
            f"color: {self._theme.text_muted}; font-size: 12px; border: none;"
        )
        layout.addWidget(version_label)

        layout.addStretch()

        self._time_label = QLabel()
        self._time_label.setStyleSheet(
            f"color: {self._theme.text_muted}; font-size: 12px; border: none;"
        )
        layout.addWidget(self._time_label)

        return footer

    def _setup_timers(self):
        for name in ("_frame_timer", "_stats_timer", "_time_timer"):
            previous = getattr(self, name, None)
            if previous is not None:
                previous.stop()
                previous.deleteLater()
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._multi_video_widget.check_expiry)
        self._frame_timer.timeout.connect(self._expire_telemetry)
        self._frame_timer.start(self.settings.ui.frame_interval_ms)

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_camera_info)
        self._stats_timer.start(self.settings.ui.stats_interval_ms)

        self._time_timer = QTimer(self)
        self._time_timer.timeout.connect(self._update_time)
        self._time_timer.start(1000)
        self._time_label.setText(time.strftime("%Y-%m-%d %H:%M:%S"))

    def _update_time(self):
        if self._services_running and self._time_label:
            self._time_label.setText(time.strftime("%Y-%m-%d %H:%M:%S"))

    def _update_camera_info(self):
        if (
            not self._services_running
            or self._multi_video_widget is None
            or self._control_panel is None
        ):
            return

        for key in CAMERA_NAMES:
            display = self._multi_video_widget.get_display(key)
            if display is None:
                continue
            fps = display.get_fps()
            w, h = display.get_resolution()
            latency_ms = display.get_latency_ms()
            self._multi_video_widget.set_camera_stats(key, fps, latency_ms)
            self._control_panel.set_camera_grid(key, fps, w, h, latency_ms)

        if self._webrtc_connected:
            front = self.fb.get(self.settings.video.primary_view)
            fresh = front is not None and front.fresh(
                self.settings.video.stale_frame_ms / 1000
            )
            self._control_panel.set_connection_status(
                "video", fresh, "传输中" if fresh else "主画面已过期"
            )

    def _connect_usb(self):
        if self._services_running and self._control_thread is not None:
            return self._control_thread.request_usb_connect()
        return False

    def _start_services(self):
        if any(
            worker is not None and worker.isRunning()
            for worker in (self._receiver_thread, self._control_thread)
        ):
            raise RuntimeError("previous workers have not stopped")
        self._service_generation += 1
        self.video_gate.invalidate("service_restarted")
        self._setup_signals()
        self._webrtc_connected = False
        try:
            self._navbar.set_control_status(STATUS_TEXT["connecting"])
            self._control_panel.set_connection_status("usb", False, "连接中")
            self._start_video()
            self._start_control()
        except Exception as exc:  # noqa: BLE001 - GUI boundary rolls back partial startup
            logger.error("服务启动失败 (%s)", type(exc).__name__)
            self._request_shutdown("logout")

    def _start_video(self):
        self._media_generation += 1
        receiver = WebRTCReceiver(
            self._room, self.settings.network, self.settings.credentials,
            video_policy=self.settings.video,
        )
        generation = self.fb.begin_session()
        receiver.on_frame(
            lambda track_id, frame, source, captured_at=None: self.fb.put(
                track_id,
                frame,
                source=source,
                generation=generation,
                captured_at=captured_at,
            ),
            include_capture_time=True,
        )

        def invalidate(source):
            if self.fb.invalidate_source(source, generation=generation):
                self.video_gate.invalidate("publisher_disconnected", source=source)

        receiver.on_invalidate(invalidate)

        self._receiver_thread = WebRTCReceiverThread(receiver)

        media_generation = self._media_generation
        self._receiver_thread.connection_changed.connect(
            lambda ok, g=media_generation: self._media_signal(
                g, self._on_video_conn, ok
            )
        )
        self._receiver_thread.error_occurred.connect(
            lambda e: logger.error(f"视频错误: {e}")
        )
        self._receiver_thread.status_changed.connect(
            lambda status, text, g=media_generation: self._media_signal(
                g, self._on_video_status_changed, status, text
            )
        )
        self._receiver_thread.finished.connect(self._finish_shutdown)
        self._receiver_thread.start()

    def _media_signal(self, generation, callback, *args):
        if generation == self._media_generation and self._services_running:
            callback(*args)

    def _reconnect_media(self):
        if (
            not self._services_running
            or self._pending_action
            or self._media_restart_pending
        ):
            return False
        self._media_restart_pending = True
        self._media_generation += 1
        self.fb.clear()
        self.video_gate.invalidate("media_restarted")
        self._clear_feedback()
        self._webrtc_connected = False
        self._control_panel.set_media_reconnecting(True)
        for name in ("signal", "webrtc"):
            self._control_panel.set_connection_status(name, False, "重连中")
        self._control_panel.set_connection_status("video", False, "等待画面")
        if self._receiver_thread is not None:
            self._receiver_thread.request_stop()
        self._shutdown_poll.start()
        self._finish_shutdown()
        return True

    def _reconnect_control(self):
        if (
            not self._services_running
            or self._pending_action
            or self._control_thread is None
        ):
            return False
        if not self._control_thread.request_control_reconnect():
            return False
        self._on_quic_status_changed(False, STATUS_TEXT["connecting"])
        return True

    def _start_control(self):
        self._control_thread = ControlSystemThread(
            self._bridge,
            replace(self.settings, session=int(self._session_id), room=self._room),
            video_gate=self.video_gate,
        )
        self._control_thread.finished.connect(self._finish_shutdown)
        self._control_thread.start()

    def _reconnect(self):
        if self._services_running and self._pending_action is None:
            self._request_shutdown("restart")

    def _on_video_conn(self, connected: bool):
        if not self._services_running or self._control_panel is None:
            return
        text = "在线" if connected else "离线"
        self._control_panel.set_connection_status("signal", connected, text)

    def _on_video_status_changed(self, status: str, message: str):
        if not self._services_running or self._multi_video_widget is None:
            return
        # if status != "receiving":
        #     self._multi_video_widget.show_message_all(message)

        # 更新控制面板的连接状态
        if self._control_panel:
            if status in {"joined", "error", "disconnected"}:
                self._control_panel.set_media_reconnecting(False)
            if status == "connecting":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "连接中")
            elif status == "joined":
                self._control_panel.set_connection_status("signal", True, "已认证")
            elif status == "waiting_peer":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", True, "已连接")
                self._control_panel.set_connection_status("webrtc", False, "等待车端")
            elif status == "peer_joined":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", True, "已连接")
                self._control_panel.set_connection_status("webrtc", False, "等待 offer")
            elif status == "receiving_offer":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("webrtc", False, "建立中")
            elif status == "connected":
                self._webrtc_connected = True
                self._control_panel.set_connection_status("webrtc", True, "已连接")
                self._control_panel.set_connection_status("video", False, "等待首帧")
            elif status == "receiving":
                # 只有 WebRTC 已连接时才更新视频状态
                if self._webrtc_connected:
                    self._control_panel.set_connection_status("video", True, "传输中")
            elif status == "peer_left":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("webrtc", False, "车端离开")
                self._control_panel.set_connection_status("video", False, "无信号")
            elif status == "disconnected":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "已断开")
                self._control_panel.set_connection_status("webrtc", False, "已断开")
                self._control_panel.set_connection_status("video", False, "无信号")
            elif status == "error":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "错误")

    def get_session_id(self) -> str:
        """获取会话ID"""
        return self._session_id

    def get_room(self) -> str:
        """获取房间号"""
        return self._room

    def get_login_data(self) -> dict:
        """获取完整的登录数据"""
        return self._login_data.copy()

    def _stop_services(self):
        self._request_shutdown("logout")

    def _logout(self):
        self._request_shutdown("logout")

    def _request_shutdown(self, action):
        if self._pending_action == "close":
            return
        self._pending_action = action
        if self._navbar is not None:
            self._navbar.set_control_status(
                "正在重新连接" if action == "restart" else "正在退出会话"
            )
        self._media_restart_pending = False
        self._media_generation += 1
        self._services_running = False
        self._service_generation += 1
        self.fb.clear()
        self.video_gate.invalidate("service_stopped")
        self._clear_feedback()
        if self._control_panel is not None:
            self._control_panel.set_local_input(None)
        self._login_page.setEnabled(False)
        self._control_page.setEnabled(False)
        for name in ("_frame_timer", "_stats_timer", "_time_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        for worker in (self._receiver_thread, self._control_thread):
            if worker is not None:
                worker.request_stop()
        self._shutdown_poll.start()
        self._finish_shutdown()

    def _finish_shutdown(self):
        if self._pending_action is None:
            if self._media_restart_pending and (
                self._receiver_thread is None or not self._receiver_thread.isRunning()
            ):
                self._shutdown_poll.stop()
                self._receiver_thread = None
                self._media_restart_pending = False
                try:
                    self._start_video()
                except Exception as exc:  # noqa: BLE001 - isolate GUI restart failures
                    logger.error("Media restart failed (%s)", type(exc).__name__)
                    self._control_panel.set_connection_status(
                        "signal", False, "重连失败"
                    )
                    self._control_panel.set_media_reconnecting(False)
            return
        if any(
            worker is not None and worker.isRunning()
            for worker in (self._receiver_thread, self._control_thread)
        ):
            return
        self._shutdown_poll.stop()
        self._receiver_thread = self._control_thread = None
        action, self._pending_action = self._pending_action, None
        if action == "close":
            self._closing_ready = True
            QTimer.singleShot(0, self.close)
        elif action == "restart":
            self._control_page.setEnabled(True)
            self._setup_timers()
            self._services_running = True
            self._start_services()
        else:
            self._finish_logout()
            self._login_page.setEnabled(True)

    def _finish_logout(self):
        # 清除会话信息
        self._session_id = None
        self._room = None
        self._login_data = {}

        # 清除 UI 组件引用（在销毁控制页面之前）
        self._control_panel = None
        self._navbar = None
        self._time_label = None

        try:
            # 销毁旧的控制页面，创建新的空容器
            self._stacked_widget.removeWidget(self._control_page)
            self._control_page.deleteLater()
            self._control_page = QWidget()
            self._stacked_widget.addWidget(self._control_page)

            # 切回登录页面
            self._login_page.reset_status()
            self._stacked_widget.setCurrentIndex(0)
            logger.info("已返回登录页面")
        except RuntimeError as exc:
            logger.error("切换到登录页面时出错 (%s)", type(exc).__name__)

    def closeEvent(self, event):
        if self._closing_ready:
            event.accept()
        else:
            event.ignore()
            self._request_shutdown("close")


def run_gui(settings, *, connect=False):
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    theme = UIFactory.set_theme(settings.ui.theme)
    app.setFont(QFont(theme.get_font_family(), 9))
    window = MainWindow(
        session_id=settings.session if connect else None,
        room=settings.room if connect else None,
        settings=settings,
    )
    window.show()
    with shutdown_signals(window):
        QTimer.singleShot(0, lambda: logger.info("Driving GUI event loop responding"))
        return app.exec()


def main(argv=None):
    from rd_driving.cli import main as entry

    return entry(argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""
远程驾驶控制系统 - 主界面

模块化 UI，使用工厂模式创建组件。
"""

import sys
import time
from dataclasses import replace

from PyQt6.QtCore import QTimer, pyqtSignal
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
from rd_driving.shutdown import shutdown_deadline, shutdown_signals
from rd_driving.ui.widgets import LoginPage
from rd_driving.ui.widgets.multi_video_widget import CAMERA_NAMES, MultiVideoWidget

logger = get_logger(__name__)

CONTROL_IN_PROGRESS = {
    "starting", "retrying", "waiting_device", "waiting_input", "waiting_neutral",
    "waiting_execution", "active",
}


class MainWindow(QMainWindow):
    """主窗口 - 包含登录页面和控制页面"""

    _close_started = pyqtSignal()

    def __init__(
        self, session_id: int | str | None = None, room: str | None = None, *, settings=None
    ):
        super().__init__()
        self.settings = settings or load_config(DrivingSettings, "driving").value
        self.video_gate = VideoGate(
            stale_frame_ms=self.settings.video.stale_frame_ms,
            recovery_ms=self.settings.video.recovery_ms,
            max_capture_age_ms=self.settings.video.max_capture_age_ms,
        )
        self._session_id = int(session_id) if session_id is not None else None
        self._room = room
        self._theme = UIFactory.set_theme(self.settings.ui.theme)

        # 服务相关
        self._receiver_thread = None
        self._control_thread = None
        self._service_generation = 0
        self._media_generation = 0
        self._video_requested = False
        self._media_stop_pending = False
        self._media_retry_delay_ms = 0
        self._media_retry_timer = QTimer(self)
        self._media_retry_timer.setSingleShot(True)
        self._media_retry_timer.timeout.connect(self._open_requested_video)
        self._quic_online = False
        self._controlled_vehicle = None
        self._control_state = "idle"
        self._control_action_pending = False
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
            (self._bridge.control_status_changed, self._on_control_status_changed),
            (self._bridge.control_target_changed, self._on_control_target_changed),
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
            if not self._quic_online or self._control_state != "active":
                return
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
            self._quic_online = ok
            self._control_panel.set_connection_status("quic", ok, text)
            self._refresh_control_action()
            if not ok:
                self._clear_feedback()
                self._on_control_target_changed(None)

    def _on_control_target_changed(self, principal: str | None):
        self._controlled_vehicle = principal if self._quic_online else None
        if self._navbar is not None:
            self._navbar.set_control_target(self._controlled_vehicle)

    def _on_control_status_changed(self, state: str, text: str):
        if not self._services_running:
            return
        if state == "active" and not self._quic_online:
            return
        self._control_state = state
        self._control_action_pending = False
        self._refresh_control_action()
        if self._control_panel is not None:
            self._control_panel.set_connection_status("control", state == "active", text)
        if self._navbar is not None:
            tone = "waiting"
            if state == "active":
                tone = "active"
            elif state in {"paused", "revoked", "lease_busy", "vehicle_changed", "disconnected"}:
                tone = "blocked"
            self._navbar.set_control_status(text, tone=tone)
            if state not in {"active", "waiting_execution"}:
                self._on_control_target_changed(None)
        if state != "active":
            self._clear_lease_feedback()

    def _refresh_control_action(self):
        if self._control_panel is None:
            return
        state = self._control_state
        requested = state in CONTROL_IN_PROGRESS
        enabled = (
            (requested or self._quic_online)
            and state != "stopping"
            and not self._control_action_pending
        )
        self._control_panel.set_control_action(requested or state == "stopping", enabled=enabled)

    def _toggle_control(self):
        worker = self._control_thread
        if worker is None or not self._services_running:
            return
        if self._control_state in CONTROL_IN_PROGRESS:
            accepted = worker.request_stop_control()
        elif self._quic_online and self._control_state != "stopping":
            accepted = worker.request_start_control()
        else:
            return
        if accepted:
            self._control_action_pending = True
            self._refresh_control_action()

    def _clear_lease_feedback(self):
        self._telemetry_at = None
        self._command_deadline = None
        self._command_confirmed_once = False
        if self._control_panel is not None:
            self._control_panel.clear_command_status()
            self._control_panel.clear_measured_status()

    def _clear_feedback(self):
        self._clear_lease_feedback()
        self._vehicle_status_deadline = None
        if self._control_panel is not None:
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
        """进入工作台并启动只读状态连接与本地输入预览。"""
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
        self._control_panel.video_toggle_requested.connect(self._toggle_video)
        self._control_panel.control_toggle_requested.connect(self._toggle_control)
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
            self._update_primary_video_status()

    def _update_primary_video_status(self):
        picture = self.video_gate.snapshot()
        if picture.ready and (
            picture.source.session != self._session_id or (
                self._controlled_vehicle is not None
                and picture.source.principal != self._controlled_vehicle
            )
        ):
            self._control_panel.set_connection_status("video", False, "画面与控制目标不符")
            return
        text = {
            "ready": "传输中",
            "recovering_picture": "主画面恢复中",
            "picture_hidden": "主画面已隐藏",
            "picture_expired": "主画面已过期",
            "capture_unknown_or_expired": "主画面延迟未知或过高",
        }.get(picture.reason, "主画面不可用")
        self._control_panel.set_connection_status("video", picture.ready, text)

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
        self._quic_online = False
        self._video_requested = False
        try:
            self._on_control_status_changed("idle", STATUS_TEXT["idle"])
            self._on_quic_status_changed(False, STATUS_TEXT["connecting"])
            self._control_panel.set_connection_status("usb", False, "连接中")
            self._multi_video_widget.show_message_all("视频未连接")
            self._start_session_worker()
        except Exception as exc:  # noqa: BLE001 - GUI boundary rolls back partial startup
            logger.error("服务启动失败 (%s)", type(exc).__name__)
            self._request_shutdown("logout")

    def _start_video(self):
        self._media_generation += 1
        receiver = WebRTCReceiver(
            self._room, self.settings.network, self.settings.credentials,
            video_policy=self.settings.video,
            low_latency_vp8=self.settings.low_latency_vp8,
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

        worker = WebRTCReceiverThread(receiver)

        media_generation = self._media_generation
        worker.error_occurred.connect(
            lambda e: logger.error(f"视频错误: {e}")
        )
        worker.status_changed.connect(
            lambda status, text, g=media_generation: self._media_signal(
                g, self._on_video_status_changed, status, text
            )
        )
        worker.finished.connect(self._on_media_worker_finished)
        worker.start()
        self._receiver_thread = worker

    def _on_media_worker_finished(self):
        if self.sender() is self._receiver_thread:
            # Qt can emit finished before isRunning() changes on the GUI thread.
            self._shutdown_poll.start()
            self._finish_shutdown()

    def _media_signal(self, generation, callback, *args):
        if generation == self._media_generation and self._services_running and self._video_requested:
            callback(*args)

    def _toggle_video(self):
        if not self._services_running or self._pending_action:
            return
        if self._video_requested:
            self._video_requested = False
            self._stop_media("video_disconnected")
        else:
            self._reconnect_media()

    def _open_requested_video(self):
        if (
            not self._services_running or not self._video_requested
            or self._pending_action or self._receiver_thread is not None
        ):
            return
        self._control_panel.set_video_requested(True)
        self._multi_video_widget.show_message_all("等待画面")
        self._control_panel.set_connection_status("video", False, "等待画面")
        self._control_panel.set_connection_status("signal", False, "连接中")
        self._control_panel.set_connection_status("webrtc", False, "等待信令")
        try:
            self._start_video()
        except Exception as exc:  # noqa: BLE001 - isolate video startup from the session
            logger.error("视频启动失败 (%s)", type(exc).__name__)
            self._reset_media("video_start_failed")
            self._schedule_media_retry()

    def _reconnect_media(self):
        if not self._services_running or self._pending_action:
            return
        self._media_retry_delay_ms = 0
        self._video_requested = True
        if self._receiver_thread is None:
            self._media_retry_timer.stop()
            self._open_requested_video()
        else:
            self._stop_media("media_restarted")

    def _stop_media(self, reason):
        self._media_retry_timer.stop()
        if self._receiver_thread is not None and not self._media_stop_pending:
            self._media_stop_pending = True
            self._receiver_thread.request_stop()
        self._reset_media(reason)
        self._shutdown_poll.start()
        self._finish_shutdown()

    def _reset_media(self, reason):
        self._media_generation += 1
        self.fb.clear()
        requested = self._video_requested
        if self._media_stop_pending:
            text = "等待重新连接视频" if requested else "正在断开视频"
            connection_text = "断开中"
        else:
            text = "等待自动重连" if requested else "视频未连接"
            connection_text = "未连接"
        self._multi_video_widget.show_message_all(text, reason=reason)
        self._webrtc_connected = False
        self._control_panel.set_video_requested(
            requested, reconnect_pending=self._media_stop_pending and requested
        )
        for name in ("signal", "webrtc"):
            self._control_panel.set_connection_status(name, False, connection_text)
        self._control_panel.set_connection_status("video", False, text)

    def _schedule_media_retry(self):
        # Live sessions own WSS and peer recovery. Only a missing worker is retried here.
        if self._media_retry_timer.isActive():
            return
        if not self._media_retry_delay_ms:
            initial_delay = min(30.0, max(0.1, self.settings.network.reconnect_delay_s))
            self._media_retry_delay_ms = round(1000 * initial_delay)
        self._media_retry_timer.start(self._media_retry_delay_ms)
        self._media_retry_delay_ms = min(30_000, 2 * self._media_retry_delay_ms)

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

    def _start_session_worker(self):
        self._control_thread = ControlSystemThread(
            self._bridge,
            replace(self.settings, session=self._session_id, room=self._room),
        )
        self._control_thread.finished.connect(self._finish_shutdown)
        self._control_thread.start()

    def _on_video_status_changed(self, status: str, message: str):
        if not self._services_running or self._multi_video_widget is None:
            return
        if self._control_panel:
            if status == "connecting":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "连接中")
                self._control_panel.set_connection_status("webrtc", False, "等待信令")
                self._control_panel.set_connection_status("video", False, "等待画面")
            elif status == "joined":
                self._media_retry_delay_ms = 0
                self._control_panel.set_connection_status("signal", True, "已认证")
            elif status == "waiting_peer":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", True, "已连接")
                self._control_panel.set_connection_status("webrtc", False, "等待车端")
                self._control_panel.set_connection_status("video", False, "等待车端")
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
                    self._update_primary_video_status()
            elif status == "peer_failed":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("webrtc", False, "连接失效")
                self._control_panel.set_connection_status("video", False, "等待自动重连")
            elif status == "peer_left":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("webrtc", False, "车端离开")
                self._control_panel.set_connection_status("video", False, "等待车端")
            elif status == "disconnected":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "已断开")
                self._control_panel.set_connection_status("webrtc", False, "已断开")
                self._control_panel.set_connection_status("video", False, "等待自动重连")
            elif status == "error":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "错误")

    def get_session_id(self) -> int | None:
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
        self._video_requested = False
        self._quic_online = False
        self._media_stop_pending = False
        self._media_retry_timer.stop()
        self._media_generation += 1
        self._services_running = False
        self._service_generation += 1
        self.video_gate.invalidate("service_stopped")
        if action == "close":
            self._close_started.emit()
        self.fb.clear()
        for worker in (self._receiver_thread, self._control_thread):
            if worker is not None:
                worker.request_stop()
        message = {
            "close": "正在关闭驾驶端",
        }.get(action, "正在退出会话")
        if self._navbar is not None:
            self._navbar.set_control_status(message)
        self._clear_feedback()
        if self._control_panel is not None:
            if action == "close":
                self._multi_video_widget.show_message_all(message)
            self._control_panel.set_local_input(None)
        self._login_page.setEnabled(False)
        self._control_page.setEnabled(False)
        for name in ("_frame_timer", "_stats_timer", "_time_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        self._shutdown_poll.start()
        self._finish_shutdown()

    def _finish_shutdown(self):
        if self._pending_action is None:
            if self._receiver_thread is not None and self._receiver_thread.isRunning():
                return
            self._shutdown_poll.stop()
            worker, self._receiver_thread = self._receiver_thread, None
            stopping, self._media_stop_pending = self._media_stop_pending, False
            if stopping:
                if self._video_requested:
                    self._open_requested_video()
                else:
                    self._reset_media("video_disconnected")
            elif worker is not None and self._video_requested and self._services_running:
                self._reset_media("media_worker_exited")
                self._schedule_media_retry()
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
    with shutdown_deadline() as arm_deadline, shutdown_signals(window):
        window._close_started.connect(arm_deadline)
        window.show()
        QTimer.singleShot(0, lambda: logger.info("Driving GUI event loop responding"))
        return app.exec()


def main(argv=None):
    from rd_driving.cli import main as entry

    return entry(argv)


if __name__ == "__main__":
    raise SystemExit(main())

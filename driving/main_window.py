"""
远程驾驶控制系统 - 主界面

模块化 UI，使用工厂模式创建组件。
"""

import sys
import time

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QStackedWidget,
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import QTimer, Qt

from factories import UIFactory
from ui.widgets import LoginPage, MultiVideoWidget, GLMultiVideoWidget

from infrastructure.workers import (
    SignalBridge,
    ControlSystemThread,
    VehicleStatusThread,
    WebRTCReceiverThread,
)
from infrastructure.video import WebRTCReceiver, FrameBuffer
from logger import get_logger
from config.loaders import get_webrtc_config, AppConfig

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    """主窗口 - 包含登录页面和控制页面"""

    def __init__(self, session_id: str = None, room: str = None):
        super().__init__()
        self._session_id = session_id
        self._room = room
        self._theme = UIFactory.get_theme()

        # 服务相关
        self._receiver_thread = None
        self._control_thread = None

        # UI 组件引用（用于安全检查）
        self._control_panel = None
        self._navbar = None
        self._time_label = None

        # 标记服务是否正在运行
        self._services_running = False

        # WebRTC 连接状态标记（只有连接成功后才允许显示视频传输状态）
        self._webrtc_connected = False
        self.fb = FrameBuffer()

        # 登录数据
        self._login_data = {}
        if session_id and room:
            self._login_data = {"session_id": session_id, "room": room}
        logger.info(self._login_data)
        self._setup_ui()

        # 如果有 session_id，直接进入主控制页面
        if session_id and room:
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
        self._bridge.usb_status_changed.connect(self._on_usb_status_changed)
        self._bridge.quic_status_changed.connect(self._on_quic_status_changed)

    def _on_usb_status_changed(self, ok: bool, text: str):
        if self._services_running and self._control_panel:
            self._control_panel.set_connection_status("usb", ok, text)

    def _on_quic_status_changed(self, ok: bool, text: str):
        if self._services_running and self._control_panel:
            self._control_panel.set_connection_status("quic", ok, text)

    def _setup_ui(self):
        self.setWindowTitle(f"{AppConfig.APP_NAME} | {AppConfig.APP_NAME_EN}")
        self.setMinimumSize(AppConfig.WINDOW_MIN_WIDTH, AppConfig.WINDOW_MIN_HEIGHT)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 使用 QStackedWidget 管理页面切换
        self._stacked_widget = QStackedWidget()

        # 页面 0: 登录页面
        self._login_page = LoginPage(self._theme)
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
        self._session_id = data.get("session_id")
        self._room = data.get("room")
        self._switch_to_control_page()

    def _switch_to_control_page(self):
        """切换到控制页面并启动服务"""
        self._setup_control_page()
        self._setup_signals()
        self._setup_timers()
        self._start_services()
        self._services_running = True
        self._stacked_widget.setCurrentIndex(1)

    def _setup_control_page(self):
        """设置控制页面的 UI"""
        layout = QVBoxLayout(self._control_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 导航栏
        self._navbar = UIFactory.create_navbar()
  
        self._navbar.set_room(self._room)
        self._navbar.add_action("退出登录", self._logout, style="secondary")
        layout.addWidget(self._navbar.get_widget())

        # 内容区 - 使用 QFrame 作为容器确保子组件等高
        content = QFrame()
        content.setStyleSheet("background: transparent; border: none;")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setSpacing(20)

        # self._multi_video_widget = GLMultiVideoWidget(self.fb)
        self._multi_video_widget = MultiVideoWidget(self.fb)
        self._control_panel = UIFactory.create_control_panel()
        self._control_panel.set_reconnect_callback(self._reconnect)

        # 直接添加组件，不设置 alignment，让 QHBoxLayout 自动拉伸高度
        content_layout.addWidget(self._multi_video_widget, 1)
        content_layout.addWidget(self._control_panel, 0)
        layout.addWidget(content, 1)

        # 底部栏
        layout.addWidget(self._create_footer())

    def _create_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("appFooter")
        footer.setFixedHeight(32)
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
        layout.setContentsMargins(20, 0, 20, 0)

        version_label = QLabel(f"v{AppConfig.VERSION}")
        version_label.setStyleSheet(
            f"color: {self._theme.text_muted}; font-size: 11px; border: none;"
        )
        layout.addWidget(version_label)

        layout.addStretch()

        self._time_label = QLabel()
        self._time_label.setStyleSheet(
            f"color: {self._theme.text_muted}; font-size: 11px; border: none;"
        )
        layout.addWidget(self._time_label)

        return footer

    def _setup_timers(self):
        self._frame_timer = QTimer()
        self._frame_timer.timeout.connect(self._multi_video_widget.update)
        self._frame_timer.start(AppConfig.FRAME_TIMER_INTERVAL)
        
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._update_camera_info)
        self._stats_timer.start(AppConfig.STATS_TIMER_INTERVAL)
        
        self._time_timer = QTimer()
        self._time_timer.timeout.connect(self._update_time)
        self._time_timer.start(AppConfig.TIME_TIMER_INTERVAL)
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

        CAMERA_NAMES = ["front", "back", "left", "right"]
        for key in CAMERA_NAMES:
            display = self._multi_video_widget.get_display(key)
            if display is None:
                continue
            fps = display.get_fps()
            w, h = display.get_resolution()
            self._control_panel.set_camera_grid(key, fps, w, h)

    def _start_services(self):
        self._receiver_thread = None
        self._control_thread = None
        self._webrtc_connected = False

        self._start_video()
        self._start_control()

    def _start_video(self):
        config = get_webrtc_config()
        receiver = WebRTCReceiver(self._room, config)
        receiver.on_frame(self.fb.put)

        self._receiver_thread = WebRTCReceiverThread(receiver)

        self._receiver_thread.connection_changed.connect(self._on_video_conn)
        self._receiver_thread.error_occurred.connect(
            lambda e: logger.error(f"视频错误: {e}")
        )
        self._receiver_thread.status_changed.connect(self._on_video_status_changed)
        self._receiver_thread.start()

    def _start_control(self):
        self._control_thread = ControlSystemThread(self._bridge, self._session_id)
        self._control_thread._state_callback = self._control_panel.set_conmand_grid
        self._control_thread.start()



    def _reconnect(self):
        if not self._services_running:
            return

        # 重置 WebRTC 连接状态
        self._webrtc_connected = False

        if self._control_panel:
            self._control_panel.set_connection_status("video", False, "重连中")
            self._control_panel.set_connection_status("webrtc", False, "重连中")
            self._control_panel.set_connection_status("signal", False, "重连中")
            self._control_panel.set_connection_status("usb", False, "重连中")
            self._control_panel.set_connection_status("quic", False, "重连中")           
        self._receiver_thread.request_stop()
        self._start_video()
        
        self._control_thread.request_stop()
        self._start_control()

    def _on_video_conn(self, connected: bool):
        if not self._services_running or self._control_panel is None:
            return
        text = "在线" if connected else "离线"
        self._control_panel.set_connection_status("signal", connected, text)
        self._control_panel.set_connection_status("webrtc", connected, text)

    def _on_video_status_changed(self, status: str, message: str):
        if not self._services_running or self._multi_video_widget is None:
            return
        # if status != "receiving":
        #     self._multi_video_widget.show_message_all(message)

        # 更新控制面板的连接状态
        if self._control_panel:
            if status == "connecting":
                self._webrtc_connected = False
                self._control_panel.set_connection_status("signal", False, "连接中")
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
                self._control_panel.set_connection_status("video", True, "传输中")
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
        """停止所有后台服务和定时器"""
        # 首先标记服务已停止，防止定时器回调访问已销毁的组件
        self._services_running = False

        # 停止所有定时器
        try:
            if hasattr(self, "_frame_timer") and self._frame_timer:
                self._frame_timer.stop()
        except Exception as e:
            logger.debug(f"停止帧定时器时出错: {e}")

        try:
            if hasattr(self, "_time_timer") and self._time_timer:
                self._time_timer.stop()
        except Exception as e:
            logger.debug(f"停止时间定时器时出错: {e}")

        try:
            if hasattr(self, "_stats_timer") and self._stats_timer:
                self._stats_timer.stop()
        except Exception as e:
            logger.debug(f"停止视频状态时出错: {e}")
        # 停止视频接收线程
        try:
            if self._receiver_thread and self._receiver_thread.isRunning():
                self._receiver_thread.request_stop()
                if not self._receiver_thread.wait(3000):
                    logger.warning("视频接收线程未能在超时内停止")
                self._receiver_thread = None
        except Exception as e:
            logger.debug(f"停止视频接收线程时出错: {e}")
            self._receiver_thread = None

        # 停止控制系统线程
        try:
            if self._control_thread and self._control_thread.isRunning():
                self._control_thread.request_stop()
                if not self._control_thread.wait(3000):
                    logger.warning("控制系统线程未能在超时内停止")
                self._control_thread = None
        except Exception as e:
            logger.debug(f"停止控制系统线程时出错: {e}")
            self._control_thread = None


    def _logout(self):
        """退出登录，返回登录页面"""
        logger.info("退出登录")

        try:
            # 停止所有服务（会设置 _services_running = False）
            self._stop_services()
        except Exception as e:
            logger.error(f"停止服务时出错: {e}")

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
            self._stacked_widget.setCurrentIndex(0)
            logger.info("已返回登录页面")
        except Exception as e:
            logger.error(f"切换到登录页面时出错: {e}")

    def closeEvent(self, event):
        self._stop_services()
        event.accept()


def run_gui(room: str = None):
    """
    启动 GUI

    Args:
        room: 房间号（如果提供则跳过登录）
        server: WebSocket 服务器地址
    """
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme = UIFactory.set_theme(AppConfig.DEFAULT_THEME)
    app.setFont(QFont(theme.get_font_family(), 9))

    if room:
        # 直接进入主界面（开发/调试模式）
        window = MainWindow(session_id=room, room=room)
    else:
        # 正常登录流程
        window = MainWindow()

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=AppConfig.APP_NAME)
    parser.add_argument("--room", help="房间号（跳过登录）")
    args = parser.parse_args()
    run_gui(args.room)

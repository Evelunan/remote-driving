"""
登录界面 (PyQt6 优化版)

简洁现代的登录页面，完全基于主题系统。
支持异步网络请求、加载状态反馈及调试模式切换。
"""

import os
import logging
import aiohttp
import asyncio
from typing import Optional, Dict, Any
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFrame,
    QGraphicsDropShadowEffect,
    QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer
from PyQt6.QtGui import QFont, QColor, QPalette

from interfaces.ui import ITheme
from config.loaders import get_app_config

logger = logging.getLogger("driving")

# 默认颜色兜底
DEFAULT_COLORS = {
    "primary": "#0969DA",
    "success": "#238636",
    "error": "#DA3633",
    "text_muted": "#8B949E",
    "bg_primary": "#0D1117",
    "bg_card": "#161B22",
    "border": "#30363D",
    "text_primary": "#C9D1D9",
    "text_secondary": "#8B949E",
}


class LoginThread(QThread):
    """登录请求线程 - 封装异步网络请求"""

    login_success = pyqtSignal(dict)
    login_failed = pyqtSignal(str)

    def __init__(self, server_url: str, username: str, password: str):
        super().__init__()
        self._server_url = server_url
        self._username = username
        self._password = password
        self._stopped = False

    def stop(self):
        """安全停止线程"""
        self._stopped = True

    def run(self):
        """运行异步任务"""
        try:
            # 创建新的事件循环用于当前线程
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._do_login())
            finally:
                loop.close()
        except Exception as e:
            if not self._stopped:
                self.login_failed.emit(f"线程内部错误: {str(e)}")

    async def _do_login(self):
        if self._stopped:
            return

        timeout = aiohttp.ClientTimeout(total=10, connect=5)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{self._server_url}/api/login"
                payload = {"username": self._username, "password": self._password}

                async with session.post(url, json=payload) as resp:
                    if self._stopped:
                        return

                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if isinstance(data, dict) and data.get("session_id"):
                                self.login_success.emit(data)
                            else:
                                self.login_failed.emit("服务器返回数据格式无效")
                        except aiohttp.ContentTypeError:
                            self.login_failed.emit("服务器返回非 JSON 数据")
                    else:
                        error_text = await resp.text()
                        self.login_failed.emit(
                            f"登录失败 ({resp.status}): {error_text}"
                        )

        except asyncio.TimeoutError:
            self.login_failed.emit("连接超时，请检查网络或服务器状态")
        except aiohttp.ClientConnectorError:
            self.login_failed.emit("无法连接到服务器，请检查地址是否正确")
        except aiohttp.ClientError as e:
            self.login_failed.emit(f"网络异常: {str(e)}")
        except Exception as e:
            logger.exception("Login unexpected error")
            self.login_failed.emit(f"未知错误: {str(e)}")


class LoginPage(QWidget):
    """登录页面"""

    login_success = pyqtSignal(dict)

    def __init__(self, theme: ITheme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = theme

        # 从配置文件读取调试模式
        self._debug_mode = get_app_config().debug_mode

        self._setup_ui()

    def _get_color(self, key: str) -> str:
        """安全获取主题颜色，带兜底"""
        try:
            val = getattr(self._theme, key)
            return val if val else DEFAULT_COLORS.get(key, "#FFFFFF")
        except AttributeError:
            return DEFAULT_COLORS.get(key, "#FFFFFF")

    def _get_font_family(self) -> str:
        """安全获取字体家族"""
        try:
            fam = self._theme.get_font_family()
            return fam if fam else "Segoe UI, Microsoft YaHei, sans-serif"
        except AttributeError:
            return "Segoe UI, Microsoft YaHei, sans-serif"

    def _setup_ui(self):
        # 基础窗口属性
        self.setMinimumSize(400, 500)
        self.setStyleSheet(f"background-color: {self._get_color('bg_primary')};")

        # 外层布局
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # 居中容器
        center_container = QWidget()
        center_container.setFixedWidth(380)
        center_container.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(center_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 弹性空间实现垂直居中
        outer_layout.addStretch(1)
        outer_layout.addWidget(center_container, alignment=Qt.AlignmentFlag.AlignCenter)
        outer_layout.addStretch(1)

        # --- Logo ---
        logo = QLabel("◎")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_font = QFont("Segoe UI Symbol", 56)
        logo_font.setStyleHint(QFont.StyleHint.SansSerif)
        logo.setFont(logo_font)
        logo.setStyleSheet(
            f"color: {self._get_color('primary')}; background: transparent;"
        )
        layout.addWidget(logo)

        layout.addSpacing(16)

        # --- 标题 ---
        title = QLabel("远程驾驶控制系统")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont(self._get_font_family(), 20)
        title_font.setWeight(QFont.Weight.Bold)
        title.setFont(title_font)
        title.setStyleSheet(
            f"color: {self._get_color('text_primary')}; background: transparent;"
        )
        layout.addWidget(title)

        # --- 副标题 ---
        subtitle = QLabel("REMOTE DRIVING CONTROL")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(
            f"""
            color: {self._get_color('text_muted')};
            font-size: 10px;
            letter-spacing: 3px;
            background: transparent;
            text-transform: uppercase;
        """
        )
        layout.addWidget(subtitle)

        layout.addSpacing(40)

        # --- 登录表单卡片 ---
        form_card = QFrame()
        form_card.setObjectName("loginFormCard")

        # 卡片样式
        card_bg = self._get_color("bg_card")
        card_border = self._get_color("border")
        form_card.setStyleSheet(
            f"""
            #loginFormCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-radius: 12px;
            }}
        """
        )

        # 阴影效果
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(30)
        shadow.setXOffset(0)
        shadow.setYOffset(8)
        shadow.setColor(QColor(0, 0, 0, 80))  # 加深阴影以增强立体感
        form_card.setGraphicsEffect(shadow)

        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(32, 32, 32, 32)
        form_layout.setSpacing(20)

        # --- WebRTC 房间号 ---
        room_label = QLabel("WebRTC房间号")
        room_label.setStyleSheet(
            f"""
            color: {self._get_color('text_secondary')};
            font-size: 12px;
            background: transparent;
            border: none;
            margin-bottom: 4px;
        """
        )
        form_layout.addWidget(room_label)

        self._room_input = QLineEdit()
        self._room_input.setPlaceholderText("请输入WebRTC房间号")
        self._room_input.setFixedHeight(44)
        self._room_input.setStyleSheet(self._get_input_style())
        self._room_input.setTextMargins(14, 0, 14, 0)
        form_layout.addWidget(self._room_input)

        # --- QUIC 会话号 ---
        session_label = QLabel("QUIC会话号")
        session_label.setStyleSheet(
            f"""
            color: {self._get_color('text_secondary')};
            font-size: 12px;
            background: transparent;
            border: none;
            margin-bottom: 4px;
        """
        )
        form_layout.addWidget(session_label)

        self._session_input = QLineEdit()
        self._session_input.setPlaceholderText("请输入QUIC会话号 (0–65535)")
        self._session_input.setFixedHeight(44)
        self._session_input.setStyleSheet(self._get_input_style())
        self._session_input.setTextMargins(14, 0, 14, 0)
        form_layout.addWidget(self._session_input)

        form_layout.addSpacing(8)

        # --- 登录按钮 ---
        self._login_btn = QPushButton("登  录")
        self._login_btn.setFixedHeight(46)
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_font = QFont(self._get_font_family(), 13)
        btn_font.setWeight(QFont.Weight.Bold)
        self._login_btn.setFont(btn_font)
        self._login_btn.setStyleSheet(self._get_button_style())
        self._login_btn.clicked.connect(self._on_login)
        form_layout.addWidget(self._login_btn)

        # --- 调试按钮 (条件显示) ---
        if self._debug_mode:
            self._fake_login_btn = QPushButton("快速进入 (调试模式)")
            self._fake_login_btn.setFixedHeight(40)
            self._fake_login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._fake_login_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent;
                    color: {self._get_color('text_secondary')};
                    border: 1px dashed {self._get_color('border')};
                    border-radius: 8px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: {self._get_color('bg_primary')};
                    color: {self._get_color('primary')};
                    border-style: solid;
                }}
                QPushButton:pressed {{
                    background-color: {self._get_color('bg_primary')};
                }}
            """
            )
            self._fake_login_btn.clicked.connect(self._on_fake_login)
            form_layout.addWidget(self._fake_login_btn)

        # --- 状态信息 ---
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setFixedHeight(20)
        self._status_label.setStyleSheet(
            f"color: {self._get_color('text_muted')}; background: transparent; font-size: 11px;"
        )
        form_layout.addWidget(self._status_label)

        layout.addWidget(form_card)

        # --- 版本信息 ---
        layout.addSpacing(24)
        version = QLabel(f"Version {get_app_config().version}")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setStyleSheet(
            f"color: {self._get_color('text_muted')}; background: transparent; font-size: 11px; opacity: 0.6;"
        )
        layout.addWidget(version)

        # --- 事件绑定 ---
        self._session_input.returnPressed.connect(self._on_login)
        self._room_input.returnPressed.connect(
            lambda: self._session_input.setFocus()
        )

    def _get_input_style(self) -> str:
        """生成输入框样式表"""
        bg = self._get_color("bg_primary")
        text = self._get_color("text_primary")
        border = self._get_color("border")
        primary = self._get_color("primary")
        muted = self._get_color("text_muted")

        return f"""
            QLineEdit {{
                background-color: {bg};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                font-size: 13px;
                selection-background-color: {primary};
                selection-color: white;
            }}
            QLineEdit:focus {{
                border: 2px solid {primary};
                padding: 1px; /* 补偿边框增加的宽度 */
            }}
            QLineEdit:disabled {{
                background-color: {bg};
                color: {muted};
                border: 1px solid {border};
            }}
            QLineEdit::placeholder {{
                color: {muted};
                opacity: 0.7;
            }}
        """

    def _get_button_style(self) -> str:
        """生成主按钮样式表"""
        primary = self._get_color("primary")
        # 简单计算 hover 颜色 (变亮 10%) - 实际项目中建议主题提供
        # 这里直接使用固定逻辑或主题方法
        try:
            if hasattr(self._theme, "get_button_style"):
                s = self._theme.get_button_style()
                if s:
                    return s
        except:
            pass

        return f"""
            QPushButton {{
                background-color: {primary};
                color: white;
                border: none;
                border-radius: 8px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #0c60d6; /* 稍微变深或变亮取决于原色 */
            }}
            QPushButton:pressed {{
                background-color: #0850b8;
            }}
            QPushButton:disabled {{
                background-color: #30363D;
                color: #8B949E;
            }}
        """

    def _on_fake_login(self):
        """模拟快速登录"""
        self._set_loading(True)
        self._status_label.setText("正在快速进入...")
        self._status_label.setStyleSheet(
            f"color: {self._get_color('text_muted')}; font-size: 11px;"
        )

        # 模拟短暂延迟
        QTimer.singleShot(300, lambda: self._emit_mock_success())

    def _emit_mock_success(self):
        self._set_loading(False)
        self._status_label.setText("登录成功 (调试)")
        self._status_label.setStyleSheet(
            f"color: {self._get_color('success')}; font-size: 11px;"
        )
        self.login_success.emit(
            {
                "session_id": "1234",
                "room": "1234",
                "vehicle_id": "TEST_001",
                "vehicle_name": "测试车辆",
                "debug_mode": True,
            }
        )

    def _on_login(self):
        """处理真实登录逻辑"""
        room = self._room_input.text().strip()
        session_id_str = self._session_input.text().strip()

        if not room:
            self._show_error("请输入WebRTC房间号")
            self._room_input.setFocus()
            return
        if not session_id_str:
            self._show_error("请输入QUIC会话号")
            self._session_input.setFocus()
            return

        # 校验 session_id 为 0–65535 的整数
        try:
            session_id = int(session_id_str)
        except ValueError:
            self._show_error("QUIC会话号必须为整数")
            self._session_input.setFocus()
            return
        if session_id < 0 or session_id > 65535:
            self._show_error("QUIC会话号范围: 0–65535")
            self._session_input.setFocus()
            return

        self._set_loading(True)
        self._status_label.setText("正在进入...")
        self._status_label.setStyleSheet(
            f"color: {self._get_color('text_muted')}; font-size: 11px;"
        )

        self._on_login_success({"session_id": session_id, "room": room})

    def _on_login_success(self, data: Dict[str, Any]):
        self._set_loading(False)
        self._status_label.setText("登录成功！正在跳转...")
        self._status_label.setStyleSheet(
            f"color: {self._get_color('success')}; font-size: 11px;"
        )
        self.login_success.emit(data)

    def _on_login_failed(self, error: str):
        self._set_loading(False)
        self._show_error(error)
        logger.warning(f"Login failed: {error}")

    def _show_error(self, message: str):
        self._status_label.setText(message)
        self._status_label.setStyleSheet(
            f"color: {self._get_color('error')}; font-size: 11px; font-weight: bold;"
        )
        # 可选：添加简单的抖动动画逻辑在这里

    def _set_loading(self, loading: bool):
        """设置加载状态"""
        self._login_btn.setEnabled(not loading)
        self._login_btn.setText("登录中..." if loading else "登  录")

        self._room_input.setEnabled(not loading)
        self._session_input.setEnabled(not loading)

        if loading:
            self._room_input.setCursor(Qt.CursorShape.WaitCursor)
            self._session_input.setCursor(Qt.CursorShape.WaitCursor)
        else:
            self._room_input.setCursor(Qt.CursorShape.IBeamCursor)
            self._session_input.setCursor(Qt.CursorShape.IBeamCursor)
            self._session_input.setFocus()

    def closeEvent(self, event):
        """窗口关闭时清理资源"""
        if self._login_thread and self._login_thread.isRunning():
            self._login_thread.stop()
            self._login_thread.wait(1000)  # 等待最多1秒
        super().closeEvent(event)

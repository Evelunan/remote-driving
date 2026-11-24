"""Session selection for the driving console."""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from rd_core.settings import DrivingSettings
from rd_driving.branding import APP_NAME, VERSION
from rd_driving.interfaces.ui import ITheme


class LoginPage(QWidget):
    login_success = pyqtSignal(dict)

    def __init__(self, theme: ITheme, parent: QWidget | None = None, *, settings=None):
        super().__init__(parent)
        self._theme = theme
        self.settings = settings or DrivingSettings()
        self._debug_mode = self.settings.ui.debug
        self._loading = False
        self._field_hints: dict[QLineEdit, tuple[QLabel, str]] = {}
        self._debug_timer = QTimer(self)
        self._debug_timer.setSingleShot(True)
        self._debug_timer.timeout.connect(self._submit_configured_session)

        self._setup_ui()
        self._room_input.setText(self.settings.room)
        self._session_input.setText(str(self.settings.session))

    def _setup_ui(self):
        self.setObjectName("sessionPage")
        self.setMinimumSize(400, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)
        outer.addStretch()

        row = QHBoxLayout()
        row.setSpacing(0)
        row.addStretch(1)

        card = QFrame()
        card.setObjectName("sessionForm")
        card.setMinimumWidth(352)
        card.setMaximumWidth(480)
        row.addWidget(card, 4)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch()

        form = QVBoxLayout(card)
        form.setContentsMargins(28, 24, 28, 24)
        form.setSpacing(0)

        header = QHBoxLayout()
        brand = QLabel(APP_NAME)
        brand.setObjectName("sessionBrand")
        header.addWidget(brand)
        header.addStretch()
        version = QLabel(f"v{VERSION}")
        version.setObjectName("sessionVersion")
        header.addWidget(version)
        form.addLayout(header)
        form.addSpacing(8)

        title = QLabel("选择驾驶会话")
        title.setObjectName("sessionTitle")
        form.addWidget(title)
        form.addSpacing(24)

        self._room_input = self._add_field(form, "WebRTC 房间号", "用于接收摄像头画面")
        form.addSpacing(14)
        self._session_input = self._add_field(
            form, "QUIC 会话号", "用于连接车辆控制，范围为 0–65535"
        )
        form.addSpacing(20)

        self._login_btn = QPushButton("进入驾驶台")
        self._login_btn.setMinimumHeight(44)
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._login_btn.setStyleSheet(self._theme.get_button_style())
        self._login_btn.clicked.connect(self._on_login)
        form.addWidget(self._login_btn)

        self._fake_login_btn = None
        if self._debug_mode:
            self._fake_login_btn = QPushButton("使用配置进入（调试）")
            self._fake_login_btn.setObjectName("sessionDebug")
            self._fake_login_btn.setMinimumHeight(40)
            self._fake_login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._fake_login_btn.clicked.connect(self._on_fake_login)
            form.addSpacing(8)
            form.addWidget(self._fake_login_btn)

        self._status_label = QLabel()
        self._status_label.setObjectName("sessionStatus")
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setMinimumHeight(20)
        self._status_label.setWordWrap(True)
        form.addSpacing(8)
        form.addWidget(self._status_label)

        self._room_input.returnPressed.connect(self._session_input.setFocus)
        self._session_input.returnPressed.connect(self._on_login)
        self.setTabOrder(self._room_input, self._session_input)
        self.setTabOrder(self._session_input, self._login_btn)
        if self._fake_login_btn is not None:
            self.setTabOrder(self._login_btn, self._fake_login_btn)

        self.setStyleSheet(self._page_style())

    def _add_field(self, form: QVBoxLayout, name: str, hint_text: str) -> QLineEdit:
        group = QVBoxLayout()
        group.setSpacing(5)
        label = QLabel(name)
        label.setObjectName("sessionFieldLabel")
        field = QLineEdit()
        field.setMinimumHeight(44)
        field.setAccessibleName(name)
        field.setAccessibleDescription(hint_text)
        label.setBuddy(field)
        hint = QLabel(hint_text)
        hint.setObjectName("sessionFieldHint")
        hint.setWordWrap(True)
        group.addWidget(label)
        group.addWidget(field)
        group.addWidget(hint)
        form.addLayout(group)
        self._field_hints[field] = (hint, hint_text)
        field.textEdited.connect(lambda: self._set_field_feedback(field))
        return field

    def _page_style(self) -> str:
        theme = self._theme
        return f"""
            QWidget#sessionPage {{
                background-color: {theme.bg_primary};
            }}
            QFrame#sessionForm {{
                background-color: {theme.bg_card};
                border: 1px solid {theme.border};
                border-radius: 8px;
            }}
            QLabel {{
                color: {theme.text_primary};
                font-family: "{theme.get_font_family()}";
                font-size: 13px;
            }}
            QLabel#sessionBrand {{
                color: {theme.primary};
                font-weight: 600;
            }}
            QLabel#sessionVersion, QLabel#sessionFieldHint, QLabel#sessionStatus {{
                color: {theme.text_secondary};
            }}
            QLabel#sessionTitle {{
                font-size: 30px;
                font-weight: 700;
            }}
            QLabel#sessionFieldLabel {{
                font-size: 14px;
                font-weight: 600;
            }}
            QLineEdit {{
                background-color: {theme.bg_primary};
                color: {theme.text_primary};
                border: 2px solid {theme.border};
                border-radius: 6px;
                padding: 0 12px;
                font-size: 16px;
                selection-background-color: {theme.primary};
                selection-color: {theme.bg_primary};
            }}
            QLineEdit:focus {{
                border-color: {theme.primary};
            }}
            QLineEdit:disabled {{
                color: {theme.text_secondary};
            }}
            QPushButton#sessionDebug {{
                background-color: transparent;
                color: {theme.text_primary};
                border: 2px solid {theme.border};
                border-radius: 6px;
                font-size: 13px;
            }}
            QPushButton#sessionDebug:hover, QPushButton#sessionDebug:pressed {{
                background-color: {theme.bg_tertiary};
            }}
            QPushButton#sessionDebug:focus {{
                border-color: {theme.primary};
            }}
            QPushButton#sessionDebug:disabled {{
                color: {theme.text_secondary};
            }}
        """

    def reset_status(self) -> None:
        """Clear feedback when returning to the form, keeping the chosen values."""
        self._debug_timer.stop()
        self._set_loading(False)
        self._set_status("")
        for field in self._field_hints:
            self._set_field_feedback(field)

    def _on_login(self):
        if self._loading:
            return
        self.reset_status()
        room = self._room_input.text().strip()
        session_text = self._session_input.text().strip()

        if not room:
            self._set_field_feedback(self._room_input, "请输入 WebRTC 房间号")
            return
        if len(room) > 128:
            self._set_field_feedback(self._room_input, "房间号最多为 128 个字符")
            return
        if not session_text:
            self._set_field_feedback(self._session_input, "请输入 QUIC 会话号")
            return
        try:
            session_id = int(session_text)
        except ValueError:
            self._set_field_feedback(self._session_input, "会话号必须为整数")
            return
        if not 0 <= session_id <= 65535:
            self._set_field_feedback(self._session_input, "会话号必须在 0–65535 之间")
            return

        self._submit(session_id, room, "正在连接所选会话…")

    def _on_fake_login(self):
        if self._loading:
            return
        self.reset_status()
        self._set_loading(True)
        self._set_status("正在使用配置进入…")
        self._debug_timer.start(300)

    def _submit_configured_session(self):
        if self.isEnabled():
            self._submit(self.settings.session, self.settings.room, "正在连接配置的会话…")
        else:
            self._set_loading(False)

    def _submit(self, session_id: int, room: str, message: str):
        self._set_loading(True)
        self._set_status(message)
        try:
            self.login_success.emit({"session_id": session_id, "room": room})
        finally:
            self._set_loading(False)

    def _show_error(self, message: str):
        self._set_status(message, error=True)

    def _set_status(self, message: str, *, error: bool = False):
        self._status_label.setText(message)
        self._status_label.setStyleSheet(f"color: {self._theme.error};" if error else "")

    def _set_field_feedback(self, field: QLineEdit, error: str = ""):
        hint, help_text = self._field_hints[field]
        text = error or help_text
        hint.setText(text)
        hint.setStyleSheet(f"color: {self._theme.error};" if error else "")
        field.setAccessibleDescription(text)
        if error:
            field.setFocus()

    def _set_loading(self, loading: bool):
        if loading == self._loading:
            return
        self._loading = loading
        self._login_btn.setEnabled(not loading)
        self._login_btn.setText("正在连接…" if loading else "进入驾驶台")
        self._room_input.setEnabled(not loading)
        self._session_input.setEnabled(not loading)
        if self._fake_login_btn is not None:
            self._fake_login_btn.setEnabled(not loading)

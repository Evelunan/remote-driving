"""驾驶台顶栏，持续显示当前会话和控制状态。"""

from collections.abc import Callable
from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..themes import BaseTheme, DarkTechTheme


class _IdentifierLabel(QLabel):
    """Elide long identifiers while keeping their full value accessible."""

    def __init__(self):
        super().__init__()
        self._identifier = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_identifier(self, identifier: str) -> None:
        self._identifier = identifier
        self.setAccessibleName(identifier)
        self.setToolTip(f"<qt>{escape(identifier)}</qt>")
        self._elide()

    def _elide(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(
                self._identifier, Qt.TextElideMode.ElideMiddle, self.contentsRect().width()
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()


class Navbar(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("drivingNavbar")
        self._actions: dict[str, QPushButton] = {}
        self._control_tone = "waiting"

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(16, 8, 16, 8)
        self._layout.setSpacing(20)

        self._title = QLabel("远程驾驶台", objectName="navTitle")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._subtitle = QLabel(objectName="navCaption")
        self._subtitle.setTextFormat(Qt.TextFormat.PlainText)
        self._subtitle.hide()
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(self._title)
        brand.addWidget(self._subtitle)
        self._layout.addLayout(brand)

        self._room_text = _IdentifierLabel()
        self._room_text.setObjectName("navValue")
        room = QFrame(objectName="navIdentity")
        room.setMinimumWidth(136)
        room.setMaximumWidth(240)
        self._room_caption = self._add_field(room, "视频房间", self._room_text)
        self._layout.addWidget(room, 1)

        self._session_text = QLabel("--", objectName="navValue")
        self._session_text.setTextFormat(Qt.TextFormat.PlainText)
        session = QFrame(objectName="navIdentity")
        self._add_field(session, "控制会话", self._session_text)
        self._layout.addWidget(session)
        self._target_text = _IdentifierLabel()
        self._target_text.setObjectName("navValue")
        target = QFrame(objectName="navIdentity")
        target.setMinimumWidth(120)
        target.setMaximumWidth(240)
        self._add_field(target, "受控车辆", self._target_text)
        self._layout.addWidget(target, 1)
        self._layout.addStretch(2)

        self._control_status = QLabel("等待连接", objectName="navControlStatus")
        self._control_status.setTextFormat(Qt.TextFormat.PlainText)
        self._control_status.setAccessibleName("控制状态")
        self._control_field = QFrame(objectName="navControlField")
        self._control_field.setMinimumWidth(212)
        self._add_field(self._control_field, "控制状态", self._control_status)
        self._layout.addWidget(self._control_field)

        self.set_room(None)
        self.set_control_target(None)
        self.apply_theme(DarkTechTheme())

    @staticmethod
    def _add_field(container: QFrame, caption: str, value: QLabel) -> QLabel:
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(1)
        label = QLabel(caption, objectName="navCaption")
        layout.addWidget(label)
        layout.addWidget(value)
        return label

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: BaseTheme) -> None:
        self._theme = theme
        self.setStyleSheet(
            f"""
            #drivingNavbar {{
                background: {theme.bg_secondary};
                border-bottom: 1px solid {theme.border};
            }}
            #navTitle {{
                color: {theme.text_primary};
                font-size: 21px;
                font-weight: 700;
            }}
            #navCaption {{
                color: {theme.text_muted};
                font-size: 12px;
            }}
            #navValue {{
                color: {theme.text_primary};
                font-size: 14px;
                font-weight: 600;
            }}
            #navIdentity {{
                border-left: 1px solid {theme.border};
            }}
            """
        )
        self._update_control_style()
        for button in self._actions.values():
            self._apply_button_style(button)

    def set_title(self, title: str, subtitle: str | None = None) -> None:
        self._title.setText(title)
        self._subtitle.setText(subtitle or "")
        self._subtitle.setVisible(subtitle is not None)

    def set_room(self, room: str | None) -> None:
        self._room_caption.setText("视频房间")
        self._room_text.set_identifier(room if room is not None else "--")

    def set_session(self, session: int | None) -> None:
        self._session_text.setText(str(session) if session is not None else "--")

    def set_control_target(self, principal: str | None) -> None:
        self._target_text.set_identifier(principal or "--")

    def set_vehicle_info(self, vehicle_id: str, vehicle_name: str | None = None) -> None:
        self._room_caption.setText("车辆")
        self._room_text.set_identifier(
            f"{vehicle_name} ({vehicle_id})" if vehicle_name else vehicle_id
        )

    def set_control_status(self, text: str, *, tone: str = "waiting") -> None:
        self._control_status.setText(text)
        self._control_status.setAccessibleDescription(text)
        if tone != self._control_tone:
            self._control_tone = tone
            self._update_control_style()

    def _update_control_style(self) -> None:
        theme = self._theme
        color = {"active": theme.success, "blocked": theme.error}.get(
            self._control_tone, theme.warning
        )
        self._control_field.setStyleSheet(
            f"""
            #navControlField {{
                background: {theme.bg_primary};
                border: 1px solid {theme.border};
                border-left: 3px solid {color};
                border-radius: 5px;
            }}
            #navControlStatus {{
                color: {color};
                font-size: 14px;
                font-weight: 600;
            }}
            """
        )

    def add_action(
        self, name: str, callback: Callable, style: str = "primary"
    ) -> QPushButton:
        self.remove_action(name)
        button = QPushButton(name)
        button.setMinimumHeight(36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setProperty("navStyle", style)
        button.clicked.connect(callback)
        self._apply_button_style(button)
        self._layout.addWidget(button)
        self._actions[name] = button
        return button

    def _apply_button_style(self, button: QPushButton) -> None:
        if button.property("navStyle") == "secondary":
            button.setStyleSheet(self._theme.get_secondary_button_style())
        else:
            button.setStyleSheet(self._theme.get_button_style())

    def remove_action(self, name: str) -> None:
        button = self._actions.pop(name, None)
        if button is not None:
            self._layout.removeWidget(button)
            button.hide()
            button.deleteLater()

    def clear_actions(self) -> None:
        for name in list(self._actions):
            self.remove_action(name)

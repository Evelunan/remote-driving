"""
导航栏组件 (PyQt6 优化版)

顶部导航栏，包含 Logo、标题、房间信息及动态操作按钮。
支持主题自适应、高 DPI 渲染及灵活的按钮管理。
"""

from typing import Optional, Callable, Dict, List, Tuple
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QWidget
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor

from interfaces.ui import ITheme

# 默认颜色兜底
DEFAULT_COLORS = {
    "primary": "#0969DA",
    "text_primary": "#C9D1D9",
    "text_muted": "#8B949E",
    "text_secondary": "#8B949E",
    "bg_secondary": "#161B22",
    "border": "#30363D",
    "bg_tertiary": "#21262D"
}

class Navbar(QFrame):
    """顶部导航栏"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme: Optional[ITheme] = None
        self._actions: Dict[str, QPushButton] = {}
        self._action_widgets: List[QWidget] = [] # 记录插入的行动作以便管理

        self.setFixedHeight(56)
        self.setObjectName("navbar")
        self._setup_ui()

    def _setup_ui(self):
        # 主布局
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(24, 0, 24, 0)
        self._layout.setSpacing(16)

        # === 左侧：品牌区域 ===
        brand_layout = QHBoxLayout()
        brand_layout.setSpacing(12)
        brand_layout.setContentsMargins(0, 8, 0, 8) # 垂直居中微调

        # Logo
        self._logo = QLabel("◎")
        self._logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 初始样式，后续会被 apply_theme 覆盖
        self._logo.setStyleSheet("color: #0969DA; font-size: 28px; font-weight: bold; border: none;")
        brand_layout.addWidget(self._logo)

        # 标题容器
        title_container = QVBoxLayout()
        title_container.setSpacing(0)
        title_container.setContentsMargins(0, 4, 0, 0) # 微调垂直位置

        self._title = QLabel("远程驾驶控制系统")
        self._title.setStyleSheet("color: #C9D1D9; font-size: 16px; font-weight: bold; border: none;")
        title_container.addWidget(self._title)

        self._subtitle = QLabel("REMOTE DRIVING CONTROL")
        self._subtitle.setStyleSheet("color: #8B949E; font-size: 9px; letter-spacing: 2px; border: none;")
        title_container.addWidget(self._subtitle)

        brand_layout.addLayout(title_container)
        
        # 将品牌区域作为一个整体加入主布局
        brand_widget = QWidget()
        brand_widget.setLayout(brand_layout)
        brand_widget.setSizePolicy(QWidget.SizePolicy.Fixed, QWidget.SizePolicy.Preferred)
        self._layout.addWidget(brand_widget)

        # 弹性空间：将右侧内容推向右边
        self._layout.addStretch(1)

        # === 右侧：房间徽章 ===
        self._room_badge = QFrame()
        self._room_badge.setObjectName("roomBadge")
        self._room_badge.setCursor(Qt.CursorShape.PointingHandCursor) # 暗示可点击或交互
        
        room_layout = QHBoxLayout(self._room_badge)
        room_layout.setContentsMargins(12, 6, 12, 6)
        room_layout.setSpacing(8)
        room_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._room_dot = QLabel("●")
        self._room_dot.setStyleSheet("color: #0969DA; font-size: 8px; border: none;")
        self._room_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        room_layout.addWidget(self._room_dot)

        self._room_text = QLabel("房间 --")
        self._room_text.setStyleSheet("color: #0969DA; font-size: 13px; font-weight: 500; border: none;")
        self._room_text.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        room_layout.addWidget(self._room_text)

        self._layout.addWidget(self._room_badge)

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: ITheme) -> None:
        """应用主题样式"""
        self._theme = theme

        # 辅助获取颜色
        def get_color(key: str, default: str) -> str:
            try:
                val = getattr(theme, key)
                return val if val else default
            except AttributeError:
                return default

        primary = get_color('primary', DEFAULT_COLORS['primary'])
        text_primary = get_color('text_primary', DEFAULT_COLORS['text_primary'])
        text_muted = get_color('text_muted', DEFAULT_COLORS['text_muted'])
        text_secondary = get_color('text_secondary', DEFAULT_COLORS['text_secondary'])
        bg_secondary = get_color('bg_secondary', DEFAULT_COLORS['bg_secondary'])
        border = get_color('border', DEFAULT_COLORS['border'])
        bg_tertiary = get_color('bg_tertiary', DEFAULT_COLORS['bg_tertiary'])

        # 1. 导航栏背景
        self.setStyleSheet(f"""
            #navbar {{
                background-color: {bg_secondary};
                border: none;
                border-bottom: 1px solid {border};
            }}
        """)

        # 2. Logo
        self._logo.setStyleSheet(f"color: {primary}; font-size: 28px; font-weight: bold; border: none;")

        # 3. 标题
        font_family = ""
        try:
            font_family = theme.get_font_family()
        except AttributeError:
            pass
        
        font_rule = f"font-family: {font_family};" if font_family else ""
        self._title.setStyleSheet(f"""
            color: {text_primary};
            font-size: 16px;
            font-weight: bold;
            {font_rule}
            border: none;
        """)

        self._subtitle.setStyleSheet(f"color: {text_muted}; font-size: 9px; letter-spacing: 2px; border: none;")

        # 4. 房间徽章
        rgb_val = self._hex_to_rgb(primary)
        self._room_badge.setStyleSheet(f"""
            #roomBadge {{
                background-color: rgba({rgb_val}, 0.12);
                border: 1px solid {primary};
                border-radius: 6px;
                padding: 2px;
            }}
            #roomBadge:hover {{
                background-color: rgba({rgb_val}, 0.2);
            }}
        """)

        self._room_dot.setStyleSheet(f"color: {primary}; font-size: 8px; border: none;")
        self._room_text.setStyleSheet(f"color: {primary}; font-size: 13px; font-weight: 500; border: none;")

        # 5. 更新已存在的动作按钮样式
        for name, btn in self._actions.items():
            # 简单重设样式，实际场景中可能需要记录按钮创建时的 style 参数
            # 这里为了简化，统一刷新为默认主样式或根据对象属性判断
            # 由于 add_action 时已经应用了主题，这里主要是为了应对运行时切换主题
            # 需要重新调用 add_action 逻辑较复杂，此处仅做基础样式更新示例
            # 最佳实践是重新构建按钮或存储样式配置
            pass 

    def set_title(self, title: str, subtitle: Optional[str] = None) -> None:
        """设置标题和副标题"""
        self._title.setText(title)
        if subtitle is not None:
            self._subtitle.setText(subtitle)
            self._subtitle.setVisible(True)
        else:
            self._subtitle.setVisible(False)

    def set_room(self, room: str) -> None:
        """设置房间号"""
        self._room_text.setText(f"房间 {room}")

    def set_vehicle_info(self, vehicle_id: str, vehicle_name: Optional[str] = None) -> None:
        """设置车辆信息显示
        
        Args:
            vehicle_id: 车辆 ID
            vehicle_name: 车辆名称 (可选)
        """
        if vehicle_name:
            # 限制长度防止布局挤压
            max_len = 15
            if len(vehicle_name) > max_len:
                vehicle_name = vehicle_name[:max_len-2] + ".."
            self._room_text.setText(f"{vehicle_name} ({vehicle_id})")
        else:
            self._room_text.setText(f"车辆 {vehicle_id}")
        
        # 切换徽章样式暗示这是车辆状态
        # 可以在这里添加逻辑改变圆点颜色表示车辆在线/离线

    def add_action(self, name: str, callback: Callable, style: str = "primary") -> QPushButton:
        """添加操作按钮
        
        Args:
            name: 按钮文本
            callback: 点击回调
            style: 按钮风格 ('primary' 或 'secondary')
            
        Returns:
            创建的 QPushButton 实例
        """
        btn = QPushButton(name)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(callback)
        btn.setObjectName(f"navBtn_{name}")
        
        self._apply_button_style(btn, style)
        
        # 插入到房间徽章之前 (即 layout 的倒数第二个位置，因为最后一个是 room_badge)
        # 当前布局结构: [Brand] [Stretch] [RoomBadge]
        # 我们要插在 Stretch 和 RoomBadge 之间
        insert_index = self._layout.count() - 1
        self._layout.insertWidget(insert_index, btn)
        
        self._actions[name] = btn
        self._action_widgets.append(btn)
        
        return btn

    def remove_action(self, name: str) -> None:
        """移除操作按钮"""
        if name not in self._actions:
            return
            
        btn = self._actions.pop(name)
        if btn:
            self._layout.removeWidget(btn)
            btn.deleteLater()
            if btn in self._action_widgets:
                self._action_widgets.remove(btn)

    def _apply_button_style(self, btn: QPushButton, style: str) -> None:
        """应用按钮样式"""
        if not self._theme:
            # 默认样式
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #0969DA;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    font-size: 13px;
                    padding: 6px 16px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #0c60d6;
                }
                QPushButton:pressed {
                    background-color: #0850b8;
                }
            """)
            return

        primary = self._get_color('primary')
        text_secondary = self._get_color('text_secondary')
        border = self._get_color('border')
        bg_tertiary = self._get_color('bg_tertiary')
        text_primary = self._get_color('text_primary')

        if style == "secondary":
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {text_secondary};
                    border: 1px solid {border};
                    border-radius: 6px;
                    font-size: 13px;
                    padding: 6px 16px;
                    font-weight: 500;
                }}
                QPushButton:hover {{
                    background-color: {bg_tertiary};
                    color: {text_primary};
                    border-color: {text_secondary};
                }}
                QPushButton:pressed {{
                    background-color: rgba(255,255,255, 0.05);
                }}
            """)
        else:
            # 尝试使用主题提供的按钮样式，否则使用默认主色
            try:
                if hasattr(self._theme, 'get_button_style'):
                    s = self._theme.get_button_style()
                    if s:
                        btn.setStyleSheet(s)
                        return
            except:
                pass
            
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {primary};
                    color: white;
                    border: none;
                    border-radius: 6px;
                    font-size: 13px;
                    padding: 6px 16px;
                    font-weight: 500;
                }}
                QPushButton:hover {{
                    opacity: 0.9;
                }}
                QPushButton:pressed {{
                    opacity: 0.8;
                }}
            """)

    def _get_color(self, key: str) -> str:
        """安全获取主题颜色"""
        try:
            val = getattr(self._theme, key)
            return val if val else DEFAULT_COLORS.get(key, "#FFFFFF")
        except AttributeError:
            return DEFAULT_COLORS.get(key, "#FFFFFF")

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> str:
        """十六进制转 RGB 字符串 (r, g, b)"""
        hex_color = hex_color.lstrip('#')
        if len(hex_color) != 6:
            return "0, 0, 0" # 错误兜底
        
        try:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return f"{r}, {g}, {b}"
        except ValueError:
            return "0, 0, 0"

    def clear_actions(self) -> None:
        """清空所有操作按钮"""
        # 复制列表避免遍历中修改
        names = list(self._actions.keys())
        for name in names:
            self.remove_action(name)
"""
现代毛玻璃主题

2024-2025 流行的设计风格，特点：
- 毛玻璃效果 (Glassmorphism)
- 柔和渐变
- 精致阴影
- 流畅圆角
- 科技感配色
"""

from .base_theme import BaseTheme


class ModernGlassTheme(BaseTheme):
    """现代毛玻璃主题"""

    def __init__(self):
        super().__init__()

    # ===== 主色调 - 赛博蓝紫 =====
    @property
    def primary(self) -> str:
        return "#6366F1"  # Indigo

    @property
    def primary_dark(self) -> str:
        return "#4F46E5"

    @property
    def primary_glow(self) -> str:
        return "#818CF8"

    @property
    def primary_light(self) -> str:
        return "#A5B4FC"

    # ===== 强调色 - 青色 =====
    @property
    def accent(self) -> str:
        return "#06B6D4"  # Cyan

    @property
    def accent_dark(self) -> str:
        return "#0891B2"

    @property
    def accent_light(self) -> str:
        return "#22D3EE"

    # ===== 危险色 - 玫红 =====
    @property
    def danger(self) -> str:
        return "#F43F5E"  # Rose

    @property
    def danger_dark(self) -> str:
        return "#E11D48"

    # ===== 背景 - 深邃蓝黑 =====
    @property
    def bg_primary(self) -> str:
        return "#0F0F1A"  # 深邃背景

    @property
    def bg_secondary(self) -> str:
        return "#16162A"

    @property
    def bg_tertiary(self) -> str:
        return "#1E1E38"

    @property
    def bg_card(self) -> str:
        return "#1A1A2F"

    @property
    def bg_elevated(self) -> str:
        return "#252545"

    # ===== 文字 =====
    @property
    def text_primary(self) -> str:
        return "#F8FAFC"

    @property
    def text_secondary(self) -> str:
        return "#94A3B8"

    @property
    def text_muted(self) -> str:
        return "#64748B"

    # ===== 边框 =====
    @property
    def border(self) -> str:
        return "rgba(148, 163, 184, 0.15)"

    @property
    def border_highlight(self) -> str:
        return "rgba(99, 102, 241, 0.5)"

    @property
    def border_glow(self) -> str:
        return "rgba(99, 102, 241, 0.3)"

    # ===== 状态 =====
    @property
    def success(self) -> str:
        return "#10B981"  # Emerald

    @property
    def warning(self) -> str:
        return "#F59E0B"  # Amber

    @property
    def error(self) -> str:
        return "#EF4444"  # Red

    @property
    def online(self) -> str:
        return "#34D399"  # Emerald light

    # ===== 渐变 =====
    @property
    def gradient_primary(self) -> str:
        return "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366F1, stop:1 #8B5CF6)"

    @property
    def gradient_accent(self) -> str:
        return "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #06B6D4, stop:1 #6366F1)"

    @property
    def gradient_card(self) -> str:
        return "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(30, 30, 56, 0.95), stop:1 rgba(22, 22, 42, 0.98))"

    # ===== 样式方法 =====

    def get_card_style(self) -> str:
        """毛玻璃卡片样式"""
        return f"""
            QFrame {{
                background: {self.gradient_card};
                border: 1px solid rgba(148, 163, 184, 0.12);
                border-radius: 16px;
            }}
            QFrame:hover {{
                border: 1px solid rgba(99, 102, 241, 0.35);
            }}
        """

    def get_button_style(self) -> str:
        """渐变按钮样式"""
        return f"""
            QPushButton {{
                background: {self.gradient_primary};
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 13px;
                font-weight: 600;
                font-family: {self.get_font_family()};
                padding: 12px 24px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {self.primary_glow}, stop:1 #A78BFA);
            }}
            QPushButton:pressed {{
                background: {self.primary_dark};
            }}
            QPushButton:disabled {{
                background: {self.bg_tertiary};
                color: {self.text_muted};
            }}
        """

    def get_secondary_button_style(self) -> str:
        """次要按钮样式 - 边框风格"""
        return f"""
            QPushButton {{
                background: transparent;
                color: {self.text_secondary};
                border: 1px solid rgba(148, 163, 184, 0.25);
                border-radius: 10px;
                font-size: 13px;
                font-weight: 500;
                font-family: {self.get_font_family()};
                padding: 10px 20px;
            }}
            QPushButton:hover {{
                background: rgba(99, 102, 241, 0.1);
                border: 1px solid rgba(99, 102, 241, 0.4);
                color: {self.text_primary};
            }}
            QPushButton:pressed {{
                background: rgba(99, 102, 241, 0.15);
            }}
        """

    def get_navbar_style(self) -> str:
        """导航栏样式 - 毛玻璃效果"""
        return f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(22, 22, 42, 0.95), stop:1 rgba(15, 15, 26, 0.98));
                border-bottom: 1px solid rgba(148, 163, 184, 0.1);
            }}
        """

    def get_glass_effect_style(self) -> str:
        """增强毛玻璃效果"""
        return f"""
            QFrame {{
                background: rgba(22, 22, 42, 0.85);
                border: 1px solid rgba(148, 163, 184, 0.1);
                border-radius: 16px;
            }}
        """

    def get_video_container_style(self) -> str:
        """视频容器样式 - 带内发光"""
        return f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(22, 22, 42, 1), stop:1 rgba(15, 15, 26, 1));
                border: 1px solid rgba(99, 102, 241, 0.2);
                border-radius: 16px;
            }}
        """

    def get_badge_style(self, color: str = None) -> str:
        """徽章样式"""
        c = color or self.primary
        return f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba({self._hex_to_rgb(c)}, 0.15),
                    stop:1 rgba({self._hex_to_rgb(c)}, 0.08));
                border: 1px solid rgba({self._hex_to_rgb(c)}, 0.4);
                border-radius: 8px;
            }}
        """

    def get_input_style(self) -> str:
        """输入框样式"""
        return f"""
            QLineEdit {{
                background: rgba(15, 15, 26, 0.8);
                color: {self.text_primary};
                border: 1px solid rgba(148, 163, 184, 0.15);
                border-radius: 10px;
                padding: 0 16px;
                font-size: 14px;
                font-family: {self.get_font_family()};
            }}
            QLineEdit:hover {{
                border: 1px solid rgba(148, 163, 184, 0.25);
            }}
            QLineEdit:focus {{
                border: 2px solid {self.primary};
                padding: 0 15px;
                background: rgba(99, 102, 241, 0.05);
            }}
            QLineEdit::placeholder {{
                color: {self.text_muted};
            }}
        """

    def get_status_text_style(self, online: bool) -> str:
        """状态文字样式"""
        color = self.online if online else self.text_muted
        weight = "600" if online else "normal"
        return f"color: {color}; font-size: 12px; font-weight: {weight};"

    def get_label_style(self, level: str = "primary") -> str:
        """标签样式"""
        colors = {
            "primary": self.text_primary,
            "secondary": self.text_secondary,
            "muted": self.text_muted,
        }
        return f"color: {colors.get(level, self.text_primary)}; font-family: {self.get_font_family()};"

    def get_footer_style(self) -> str:
        """底部状态栏样式"""
        return f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(22, 22, 42, 0.95), stop:1 rgba(15, 15, 26, 1));
                border-top: 1px solid rgba(148, 163, 184, 0.08);
            }}
        """

    def get_scrollbar_style(self) -> str:
        """滚动条样式"""
        return f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 4px 0;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(148, 163, 184, 0.2);
                border-radius: 4px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(99, 102, 241, 0.4);
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """

    def get_stat_value_style(self) -> str:
        """统计数值样式 - 带渐变文字效果"""
        return f"""
            color: {self.text_primary};
            font-size: 24px;
            font-weight: 700;
            font-family: {self.get_font_family()};
        """

    def get_stat_label_style(self) -> str:
        """统计标签样式"""
        return f"""
            color: {self.text_muted};
            font-size: 11px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        """

    def get_connection_item_style(self, online: bool) -> str:
        """连接项背景样式"""
        if online:
            return f"""
                QFrame {{
                    background: rgba(16, 185, 129, 0.08);
                    border-radius: 8px;
                    padding: 4px 8px;
                }}
            """
        return "background: transparent;"

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> str:
        """将十六进制颜色转换为 RGB 字符串"""
        hex_color = hex_color.lstrip('#')
        if len(hex_color) == 6:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return f"{r}, {g}, {b}"
        return "99, 102, 241"  # 默认返回 primary 色

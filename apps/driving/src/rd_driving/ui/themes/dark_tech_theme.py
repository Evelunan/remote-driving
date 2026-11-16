"""
深色科技主题

现代科技风格的深色主题，特点：
- 主色调：电动蓝 (#00A8FF)
- 强调色：能量绿 (#00E676)
- 背景：深空灰渐变
- 玻璃拟态效果
- 微妙的发光和动画效果
"""

from .base_theme import BaseTheme


class DarkTechTheme(BaseTheme):
    """深色科技主题"""

    def __init__(self):
        super().__init__()

    # ===== 主色调 - 电动蓝 =====
    @property
    def primary(self) -> str:
        return "#00A8FF"

    @property
    def primary_dark(self) -> str:
        return "#0078D4"

    @property
    def primary_glow(self) -> str:
        return "#00D4FF"

    # ===== 强调色 - 能量绿 =====
    @property
    def accent(self) -> str:
        return "#00E676"

    @property
    def accent_dark(self) -> str:
        return "#00C853"

    # ===== 危险/刹车 - 红色 =====
    @property
    def danger(self) -> str:
        return "#FF3D00"

    @property
    def danger_dark(self) -> str:
        return "#DD2C00"

    # ===== 背景 - 深空灰 =====
    @property
    def bg_primary(self) -> str:
        return "#0D1117"

    @property
    def bg_secondary(self) -> str:
        return "#161B22"

    @property
    def bg_tertiary(self) -> str:
        return "#21262D"

    @property
    def bg_card(self) -> str:
        return "#1C2128"

    # ===== 文字 =====
    @property
    def text_primary(self) -> str:
        return "#ECEFF4"

    @property
    def text_secondary(self) -> str:
        return "#8B949E"

    @property
    def text_muted(self) -> str:
        return "#6E7681"

    # ===== 边框 =====
    @property
    def border(self) -> str:
        return "#30363D"

    @property
    def border_highlight(self) -> str:
        return "#388BFD"

    # ===== 状态 =====
    @property
    def success(self) -> str:
        return "#2EA043"

    @property
    def warning(self) -> str:
        return "#D29922"

    @property
    def error(self) -> str:
        return "#F85149"

    @property
    def online(self) -> str:
        return "#3FB950"

    # ===== 增强样式方法 =====

    def get_card_style(self) -> str:
        """玻璃拟态卡片样式"""
        return f"""
            QFrame {{
                background-color: rgba(28, 33, 40, 0.95);
                border: 1px solid rgba(48, 54, 61, 0.8);
                border-radius: 12px;
            }}
            QFrame:hover {{
                border: 1px solid rgba(56, 139, 253, 0.4);
            }}
        """

    def get_button_style(self) -> str:
        """渐变按钮样式"""
        return f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {self.primary}, stop:1 {self.primary_dark});
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
                font-family: {self.get_font_family()};
                padding: 10px 20px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {self.primary_glow}, stop:1 {self.primary});
            }}
            QPushButton:pressed {{
                background: {self.primary_dark};
            }}
            QPushButton:disabled {{
                background: {self.bg_tertiary};
                color: {self.text_muted};
            }}
        """

    def get_navbar_style(self) -> str:
        """导航栏样式 - 带底部发光"""
        return f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {self.bg_secondary}, stop:1 rgba(22, 27, 34, 0.95));
                border-bottom: 1px solid {self.border};
            }}
        """

    def get_glass_effect_style(self) -> str:
        """增强的玻璃拟态效果"""
        return f"""
            QFrame {{
                background-color: rgba(22, 27, 34, 0.85);
                border: 1px solid rgba(48, 54, 61, 0.6);
                border-radius: 12px;
            }}
        """

    def get_video_container_style(self) -> str:
        """视频容器样式"""
        return f"""
            QFrame {{
                background-color: {self.bg_secondary};
                border: 1px solid {self.border};
                border-radius: 12px;
            }}
        """

    def get_badge_style(self, color: str = None) -> str:
        """徽章样式"""
        c = color or self.primary
        return f"""
            QFrame {{
                background-color: rgba({self._hex_to_rgb(c)}, 0.1);
                border: 1px solid {c};
                border-radius: 6px;
            }}
        """

    def get_status_text_style(self, online: bool) -> str:
        """状态文字样式"""
        color = self.online if online else self.text_muted
        weight = "bold" if online else "normal"
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
                background-color: {self.bg_secondary};
                border-top: 1px solid {self.border};
            }}
        """

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> str:
        """将十六进制颜色转换为 RGB 字符串"""
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return f"{r}, {g}, {b}"

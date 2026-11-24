"""配色配置 modern_glass，采用靛蓝色主色与青色强调色。"""

from .base_theme import BaseTheme


class ModernGlassTheme(BaseTheme):
    primary = "#A5B4FC"
    primary_dark = "#8593E8"
    primary_glow = "#C7D0FF"
    accent = "#72D8E8"
    accent_dark = "#54B3C3"
    border_highlight = primary

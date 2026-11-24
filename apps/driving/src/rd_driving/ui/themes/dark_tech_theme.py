"""配色配置 dark_tech，采用冷蓝色主色与绿色强调色。"""

from .base_theme import BaseTheme


class DarkTechTheme(BaseTheme):
    primary = "#86CFFF"
    primary_dark = "#66B8EC"
    primary_glow = "#BDE7FF"
    accent = "#82D6AA"
    accent_dark = "#65BA8D"
    border_highlight = primary

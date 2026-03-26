"""
控制面板组件 (PyQt6 优化版)

右侧控制面板，包含连接状态、车辆状态和实时数据。
支持动态内容管理、智能占位符显示及主题自适应。
"""

from typing import Optional, Dict, Callable, Any
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QScrollArea, QWidget,
    QPushButton, QGridLayout, QLabel, QSizePolicy, QSpacerItem
)
from PyQt6.QtCore import Qt, QObject
from PyQt6.QtGui import QCursor

from interfaces.ui import IConnectionItem, IStatItem, ITheme
from ..components import ConnectionItem, DataCard, StatItem

# 默认配置常量
CONFIG = {
    "panel_width": 300,
    "margin_outer": 8,
    "spacing_outer": 0,
    "spacing_inner": 16,
    "reconnect_btn_height": 38,
}

class ControlPanel(QFrame):
    """控制面板 - 封装滚动区域以提供一致的侧边栏体验"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme: Optional[ITheme] = None
        self._reconnect_callback: Optional[Callable[[], None]] = None
        self._is_reconnecting = False

        # 基础配置
        self.setObjectName("controlPanel")
        self.setFixedWidth(CONFIG["panel_width"])
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self._setup_ui()

    def _setup_ui(self):
        # === 外层布局 (包含内边距) ===
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(
            CONFIG["margin_outer"], 
            CONFIG["margin_outer"], 
            CONFIG["margin_outer"], 
            CONFIG["margin_outer"]
        )
        outer_layout.setSpacing(CONFIG["spacing_outer"])

        # === 滚动区域 ===
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 移除默认边框，背景由内部 Widget 或透明决定
        self._scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # 关键：确保滚动区域内容背景透明，避免白色块遮挡父容器背景
        self._scroll_area.viewport().setAutoFillBackground(False)

        # === 主面板容器 ===
        self._panel = QWidget()
        self._panel.setObjectName("controlPanelContent")
        self._panel.setStyleSheet("background: transparent;")
        
        self._layout = QVBoxLayout(self._panel)
        self._layout.setContentsMargins(0, 0, 4, 0) # 右侧留一点空隙避免贴边
        self._layout.setSpacing(CONFIG["spacing_inner"])

        # --- 1. 系统连接卡片 ---
        self._conn_card = DataCard("系统连接")
        
        # 预定义连接项
        self._connections: Dict[str, ConnectionItem] = {}
        default_connections = [
            ("signal", "信令服务器"),
            ("webrtc", "WebRTC"),
            ("video", "视频流"),
            ("usb", "控制设备"),
            ("quic", "数据通道"),
        ]
        
        for key, label in default_connections:
            conn_item = ConnectionItem(label)
            self._connections[key] = conn_item
            self._conn_card.add_widget(conn_item)

        # 重连按钮
        self._reconnect_btn = QPushButton("重新连接")
        self._reconnect_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._reconnect_btn.setFixedHeight(CONFIG["reconnect_btn_height"])
        self._reconnect_btn.clicked.connect(self._on_reconnect)
        self._conn_card.add_widget(self._reconnect_btn)

        self._layout.addWidget(self._conn_card)

        # --- 2. 车辆状态卡片 ---
        self._vehicle_card = DataCard("车辆状态")
        
        vehicle_grid = QGridLayout()
        vehicle_grid.setSpacing(12)
        # 设置列拉伸，使两个统计项平分宽度
        vehicle_grid.setColumnStretch(0, 1)
        vehicle_grid.setColumnStretch(1, 1)

        self._vehicle_stats: Dict[str, StatItem] = {
            "speed": StatItem("车速", "km/h"),
            "battery": StatItem("电量", "%"),
            "gear": StatItem("档位", ""),
            "signal": StatItem("信号", ""),
        }

        # 网格布局添加
        vehicle_grid.addWidget(self._vehicle_stats["speed"], 0, 0)
        vehicle_grid.addWidget(self._vehicle_stats["battery"], 0, 1)
        vehicle_grid.addWidget(self._vehicle_stats["gear"], 1, 0)
        vehicle_grid.addWidget(self._vehicle_stats["signal"], 1, 1)

        self._vehicle_card.add_layout(vehicle_grid)

        # 占位提示 (初始可见)
        self._vehicle_placeholder = QLabel("等待服务器数据...")
        self._vehicle_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vehicle_placeholder.setStyleSheet("color: #6E7681; font-size: 11px; padding-top: 8px; border: none;")
        self._vehicle_card.add_widget(self._vehicle_placeholder)
        self._vehicle_data_active = False # 标记是否有真实数据

        self._layout.addWidget(self._vehicle_card)

        # --- 3. 实时数据卡片 ---
        self._stats_card = DataCard("实时数据")
        
        stats_grid = QGridLayout()
        stats_grid.setSpacing(16)
        stats_grid.setColumnStretch(0, 1)
        stats_grid.setColumnStretch(1, 1)

        self._stats: Dict[str, StatItem] = {
            "fps": StatItem("帧率", "FPS"),
            "resolution": StatItem("分辨率", ""),
            "latency": StatItem("延迟", "ms"),
        }

        stats_grid.addWidget(self._stats["fps"], 0, 0)
        stats_grid.addWidget(self._stats["resolution"], 0, 1)
        stats_grid.addWidget(self._stats["latency"], 1, 0)
        
        self._stats_card.add_layout(stats_grid)
        self._layout.addWidget(self._stats_card)

        # 底部弹性空间，将内容推向顶部
        self._layout.addStretch(1)

        # 组装
        self._scroll_area.setWidget(self._panel)
        outer_layout.addWidget(self._scroll_area)

    def get_widget(self) -> QWidget:
        return self

    def apply_theme(self, theme: ITheme) -> None:
        """应用主题样式"""
        self._theme = theme

        # 1. 外层 Frame 样式
        try:
            bg_color = theme.bg_secondary
            border_color = theme.border
            
            self.setStyleSheet(f"""
                #controlPanel {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: 12px;
                }}
            """)
        except AttributeError:
            # 兜底样式
            self.setStyleSheet("""
                #controlPanel {
                    background-color: #161B22;
                    border: 1px solid #30363D;
                    border-radius: 12px;
                }
            """)

        # 2. 滚动条样式
        scrollbar_style = ""
        if hasattr(theme, 'get_scrollbar_style') and callable(theme.get_scrollbar_style):
            scrollbar_style = theme.get_scrollbar_style()
        
        if scrollbar_style:
            self._scroll_area.setStyleSheet(f"""
                QScrollArea {{
                    border: none;
                    background: transparent;
                }}
                {scrollbar_style}
            """)
        else:
            # 默认极简滚动条
            self._scroll_area.setStyleSheet("""
                QScrollArea { border: none; background: transparent; }
                QScrollBar:vertical {
                    border: none;
                    background: transparent;
                    width: 8px;
                    margin: 0;
                }
                QScrollBar::handle:vertical {
                    background: rgba(255, 255, 255, 0.2);
                    border-radius: 4px;
                    min-height: 20px;
                }
                QScrollBar::handle:vertical:hover {
                    background: rgba(255, 255, 255, 0.3);
                }
            """)

        # 3. 子组件主题应用
        cards = [self._conn_card, self._vehicle_card, self._stats_card]
        for card in cards:
            if hasattr(card, 'apply_theme'):
                card.apply_theme(theme)

        all_items = list(self._connections.values()) + list(self._vehicle_stats.values()) + list(self._stats.values())
        for item in all_items:
            if hasattr(item, 'apply_theme'):
                item.apply_theme(theme)

        # 4. 按钮样式
        btn_style = ""
        if hasattr(theme, 'get_button_style') and callable(theme.get_button_style):
            btn_style = theme.get_button_style()
        
        if btn_style:
            self._reconnect_btn.setStyleSheet(btn_style)
        else:
            # 兜底按钮样式
            self._reconnect_btn.setStyleSheet("""
                QPushButton {
                    background-color: #238636;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background-color: #2ea043;
                }
                QPushButton:pressed {
                    background-color: #1a7f32;
                }
                QPushButton:disabled {
                    background-color: #30363D;
                    color: #8B949E;
                }
            """)
        
        self._reconnect_btn.setFixedHeight(CONFIG["reconnect_btn_height"])

        # 5. 占位文字样式
        muted_color = theme.text_muted if hasattr(theme, 'text_muted') else "#6E7681"
        self._vehicle_placeholder.setStyleSheet(f"color: {muted_color}; font-size: 11px; padding-top: 8px; border: none;")

    def set_reconnect_callback(self, callback: Callable[[], None]) -> None:
        """设置重连回调函数"""
        self._reconnect_callback = callback

    def _on_reconnect(self) -> None:
        """处理重连按钮点击"""
        if self._is_reconnecting:
            return
        
        if self._reconnect_callback:
            self._is_reconnecting = True
            self._reconnect_btn.setEnabled(False)
            self._reconnect_btn.setText("重连中...")
            
            try:
                self._reconnect_callback()
            finally:
                # 假设回调是异步的，这里只是立即恢复按钮状态
                # 实际场景中可能需要回调通知完成，或者由外部逻辑控制按钮状态
                # 这里简单模拟：稍后恢复或期望外部调用 set_reconnecting_status
                self._is_reconnecting = False
                self._reconnect_btn.setEnabled(True)
                self._reconnect_btn.setText("重新连接")

    def set_reconnecting_status(self, is_reconnecting: bool) -> None:
        """外部调用：更新重连按钮状态"""
        self._is_reconnecting = is_reconnecting
        self._reconnect_btn.setEnabled(not is_reconnecting)
        self._reconnect_btn.setText("重连中..." if is_reconnecting else "重新连接")

    # === 数据更新接口 ===

    def set_connection_status(self, name: str, online: bool, text: Optional[str] = None) -> None:
        """更新指定连接的状态"""
        if name in self._connections:
            self._connections[name].set_status(online, text)
        # 如果名称不存在，可以选择静默忽略或打印警告，避免崩溃

    def set_stat_value(self, name: str, value: str) -> None:
        """更新统计项的值"""
        target_dict = None
        if name in self._stats:
            target_dict = self._stats
        elif name in self._vehicle_stats:
            target_dict = self._vehicle_stats
            
        if target_dict:
            target_dict[name].set_value(value)
            # 如果是车辆数据，自动隐藏占位符
            if name in self._vehicle_stats and not self._vehicle_data_active:
                self.set_vehicle_data_active(True)

    def set_vehicle_data_active(self, active: bool) -> None:
        """控制车辆状态占位符的显示"""
        if self._vehicle_data_active == active:
            return
            
        self._vehicle_data_active = active
        self._vehicle_placeholder.setVisible(not active)
        
        # 可选：如果隐藏占位符后卡片太空，可以调整间距，但目前布局会自动处理

    # === 动态管理接口 ===

    def add_connection(self, key: str, label: str) -> ConnectionItem:
        """动态添加一个新的连接监控项"""
        if key in self._connections:
            return self._connections[key]
            
        item = ConnectionItem(label)
        self._connections[key] = item
        self._conn_card.add_widget(item)
        
        if self._theme and hasattr(item, 'apply_theme'):
            item.apply_theme(self._theme)
            
        return item

    def remove_connection(self, key: str) -> None:
        """移除一个连接监控项"""
        if key not in self._connections:
            return
            
        item = self._connections.pop(key)
        # 从布局中移除并销毁
        self._conn_card._layout.removeWidget(item)
        item.deleteLater()

    def get_connection_items(self) -> Dict[str, IConnectionItem]:
        return self._connections

    def get_stat_items(self) -> Dict[str, IStatItem]:
        return {**self._stats, **self._vehicle_stats}
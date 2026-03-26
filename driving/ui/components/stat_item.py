"""
统计数据项组件 (优化版)

显示带标签和单位的统计数据，支持动态切换单位、主题应用及多种数据类型输入。
"""

from typing import Optional, Union
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from interfaces.ui import ITheme

# 定义默认样式常量，避免魔法字符串散落在代码中
DEFAULT_STYLES = {
    "label": "color: #6E7681; font-size: 10px; text-transform: uppercase;",
    "value": "color: #ECEFF4; font-size: 20px; font-weight: bold;",
    "unit": "color: #8B949E; font-size: 11px;",
}

class StatItem(QWidget):
    """统计数据项"""

    def __init__(self, label: str, unit: str = ""):
        super().__init__()
        self._label_text = label
        self._unit_text = unit
        self._value_text = "--"
        self._theme: Optional[ITheme] = None

        # 组件引用
        self._label: QLabel = QLabel()
        self._value: QLabel = QLabel()
        self._unit: Optional[QLabel] = None
        
        # 布局引用 (用于动态插入单位)
        self._val_layout: Optional[QHBoxLayout] = None

        self._setup_ui()

    def _setup_ui(self):
        # 主布局：垂直排列
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)

        # 1. 标签部分
        self._label.setText(self._label_text)
        self._label.setStyleSheet(DEFAULT_STYLES["label"])
        main_layout.addWidget(self._label)

        # 2. 数值行布局：水平排列
        self._val_layout = QHBoxLayout()
        self._val_layout.setSpacing(4)
        self._val_layout.setContentsMargins(0, 0, 0, 0)

        # 数值标签
        self._value.setText(self._value_text)
        self._value.setStyleSheet(DEFAULT_STYLES["value"])
        self._val_layout.addWidget(self._value)

        # 单位标签 (如果初始有单位)
        if self._unit_text:
            self._create_unit_label()

        # 添加弹簧，使内容靠左 (可根据需求改为加居中或靠右)
        self._val_layout.addStretch()
        
        main_layout.addLayout(self._val_layout)

    def _create_unit_label(self):
        """内部方法：创建单位标签并加入布局"""
        if self._unit is None and self._val_layout is not None:
            self._unit = QLabel(self._unit_text)
            self._unit.setStyleSheet(DEFAULT_STYLES["unit"])
            # 插入到数值之后，弹簧之前 (索引 1)
            self._val_layout.insertWidget(1, self._unit)

    def _remove_unit_label(self):
        """内部方法：移除单位标签"""
        if self._unit is not None and self._val_layout is not None:
            self._val_layout.removeWidget(self._unit)
            self._unit.deleteLater()  # 安全删除 Qt 对象
            self._unit = None

    def apply_theme(self, theme: ITheme) -> None:
        """应用主题，动态更新颜色"""
        self._theme = theme
        
        # 使用 try-except 防止主题对象属性缺失导致崩溃
        try:
            label_color = theme.text_muted
            value_color = theme.text_primary
            unit_color = theme.text_secondary
        except AttributeError:
            # 如果主题对象不符合预期，回退到默认行为或抛出更明确的错误
            return 

        # 更新样式：只替换颜色部分，保留字体大小等其他属性
        # 这里采用重新设置完整 styleSheet 的方式以确保覆盖之前的硬编码颜色
        self._label.setStyleSheet(f"color: {label_color}; font-size: 10px; text-transform: uppercase;")
        self._value.setStyleSheet(f"color: {value_color}; font-size: 20px; font-weight: bold;")
        
        if self._unit:
            self._unit.setStyleSheet(f"color: {unit_color}; font-size: 11px;")

    def set_value(self, value: Union[str, int, float, None]) -> None:
        """
        设置数值。支持字符串、数字或 None。
        None 或空值将显示为 '--'。
        """
        if value is None or (isinstance(value, str) and value.strip() == ""):
            display_text = "--"
        else:
            # 自动格式化数字，避免过长的小数
            if isinstance(value, float):
                # 智能格式化：如果是整数则不显示小数，否则最多保留2位
                display_text = f"{value:.2f}".rstrip('0').rstrip('.')
            else:
                display_text = str(value)
        
        self._value_text = display_text
        self._value.setText(display_text)

    def get_value(self) -> str:
        return self._value_text

    def set_label(self, label: str) -> None:
        self._label_text = label
        self._label.setText(label)

    def set_unit(self, unit: str) -> None:
        """
        动态设置单位。
        支持从无单位变为有单位，或从有单位变为无单位。
        """
        self._unit_text = unit
        
        if unit:
            if self._unit is None:
                # 之前没有单位，现在需要创建
                self._create_unit_label()
            else:
                # 已有单位，仅更新文本
                self._unit.setText(unit)
        else:
            if self._unit is not None:
                # 之前有单位，现在需要移除
                self._remove_unit_label()

    def get_widget(self) -> QWidget:
        return self
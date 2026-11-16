"""
项目常量定义

包含项目级别的常量配置和路径定义
"""

from pathlib import Path

# 项目根目录路径（相对于 utils 目录的父目录）
ROOT_PATH = Path(__file__).parent.parent.resolve()

# 配置文件路径
CONFIG_PATH = ROOT_PATH / "config"


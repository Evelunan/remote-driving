"""
项目常量定义

包含项目级别的常量配置和路径定义
"""

from pathlib import Path

# 项目根目录路径（相对于 utils 目录的父目录）
ROOT_PATH = Path(__file__).parent.parent.resolve()

# 配置文件路径
CONFIG_DIR = ROOT_PATH / "config"
SERVER_CONFIG_FILE = CONFIG_DIR / "server.yaml"
LOG_CONFIG_FILE = CONFIG_DIR / "log.yaml"


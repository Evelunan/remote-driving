"""
配置加载器模块

提供配置加载功能：
- get_server_config: 获取服务器配置（单例）
- ServerConfig: 服务器配置类
"""

from .server_loader import ServerConfig, get_server_config

__all__ = ["ServerConfig", "get_server_config"]


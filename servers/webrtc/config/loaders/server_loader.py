"""
服务器配置加载器

从 YAML 配置文件读取服务器配置，提供单例模式确保只初始化一次，
并返回不可变的配置对象。
"""

import yaml
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from logger import get_logger
from common import SERVER_CONFIG_FILE

logger = get_logger(__name__)

# 单例实例
_instance: Optional["ServerConfig"] = None


@dataclass(frozen=True)
class ServerConfig:
    """
    服务器配置类（不可变）
    
    使用 frozen=True 确保配置对象创建后不能被修改
    """
    host: str
    port: int
    max_peers_per_room: int
    ping_interval: float
    ping_timeout: float
    max_message_size: int


def _load_config_from_file(config_path: Path) -> dict:
    """
    从 YAML 文件加载配置
    
    参数:
        config_path: 配置文件路径
        
    返回:
        配置字典
        
    异常:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: YAML 解析错误
    """
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    if not isinstance(config, dict):
        raise ValueError(f"配置文件格式错误，期望字典类型: {config_path}")
    
    return config


def _create_config(config_dict: dict) -> ServerConfig:
    """
    从配置字典创建 ServerConfig 对象
    
    参数:
        config_dict: 配置字典
        
    返回:
        ServerConfig 实例
    """
    return ServerConfig(
        host=str(config_dict["host"]),
        port=int(config_dict["port"]),
        max_peers_per_room=int(config_dict["max_peers_per_room"]),
        ping_interval=float(config_dict["ping_interval"]),
        ping_timeout=float(config_dict["ping_timeout"]),
        max_message_size=int(config_dict["max_message_size"]),
    )


def get_server_config(config_path: Optional[Path] = None) -> ServerConfig:
    """
    获取服务器配置（单例模式）
    
    此函数保证：
    1. 只初始化一次（单例模式）
    2. 返回的配置对象不可变（frozen dataclass）
    
    参数:
        config_path: 可选的配置文件路径，仅在首次调用时生效
                    如果为 None，使用默认路径 SERVER_CONFIG_FILE
        
    返回:
        ServerConfig 实例（不可变）
        
    异常:
        FileNotFoundError: 配置文件不存在
        KeyError: 配置文件缺少必需的字段
    """
    global _instance
    
    # 如果已经初始化，直接返回
    if _instance is not None:
        return _instance
    
    # 确定配置文件路径
    if config_path is None:
        config_path = SERVER_CONFIG_FILE
    
    logger.info(f"正在加载服务器配置: {config_path}")
    
    # 加载配置
    config_dict = _load_config_from_file(config_path)
    
    # 创建配置对象
    _instance = _create_config(config_dict)
    
    return _instance


if __name__ == "__main__":
    # 测试代码
    config = get_server_config()
    logger.info(f"host: {config.host}")
    logger.info(f"port: {config.port}")
    logger.info(f"max_peers_per_room: {config.max_peers_per_room}")
    
    # 验证单例
    config2 = get_server_config()
    assert config is config2, "单例模式失败"
    logger.info("✅ 单例模式验证通过")
    
    # 验证不可变性（应该会抛出异常）
    try:
        # 这行代码会抛出异常，因为 frozen=True
        config.host = "127.0.0.1"  # 取消注释会抛出 FrozenInstanceError
        pass
    except Exception as e:
        logger.info(f"✅ 不可变性验证通过: {type(e).__name__}")

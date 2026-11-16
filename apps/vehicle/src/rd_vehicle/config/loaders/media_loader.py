# media_config.py
from dataclasses import dataclass
from typing import Optional
import yaml
from pathlib import Path

from rd_core.logging import get_logger
from rd_vehicle.common.constants import CONFIG_PATH

logger = get_logger(__name__)

MEDIA_CONFIG_PATH = CONFIG_PATH / "media.yaml"


@dataclass(frozen=True)
class Camera:
    name: str
    vid: str
    pid: str
    width: int = 640
    height: int = 480
    fps: int = 30


# 缓存变量（模块级单例）
_cameras: Optional[list[Camera]] = None


def _load_config(config_path: Path):
    """内部函数：从 YAML 加载并解析配置，赋值给缓存变量"""
    global _cameras

    if _cameras is not None:
        return  # 已加载，直接返回
    if config_path is None:
        config_path = MEDIA_CONFIG_PATH

    logger.info(f"Loading media config from {config_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"Media config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Parse cameras (resolution now lives under each camera)
    cameras_data = data.get("cameras", {})
    _cameras = [
        Camera(
            name=name,
            vid=info["vid"],
            pid=info["pid"],
            width=int(info.get("width", 640)),
            height=int(info.get("height", 480)),
            fps=int(info.get("fps", 30)),
        )
        for name, info in cameras_data.items()
    ]

    logger.debug(f"Loaded cameras: {[c.name for c in _cameras]}")


def get_cameras_config(config_path: Optional[Path] = None) -> list[Camera]:
    """获取摄像头配置"""
    _load_config(config_path)
    return _cameras  # type: ignore

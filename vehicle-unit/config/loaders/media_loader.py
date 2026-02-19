# media_config.py
from dataclasses import dataclass
from typing import Optional
import yaml
from pathlib import Path

from logger import get_logger
from common.constants import CONFIG_PATH

logger = get_logger(__name__)

MEDIA_CONFIG_PATH = CONFIG_PATH / "media.yaml"


@dataclass(frozen=True)
class Resolution:
    width: int
    height: int
    fps: int


@dataclass(frozen=True)
class Camera:
    name: str
    vid: str
    pid: str


# 缓存变量（模块级单例）
_resolution: Optional[Resolution] = None
_cameras: Optional[list[Camera]] = None


def _load_config(config_path: Path):
    """内部函数：从 YAML 加载并解析配置，赋值给缓存变量"""
    global _resolution, _cameras

    if _resolution is not None and _cameras is not None:
        return  # 已加载，直接返回
    if config_path is None:
        config_path = MEDIA_CONFIG_PATH

    logger.info(f"Loading media config from {config_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"Media config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Parse resolution
    res_data = data.get("resolution", {})
    _resolution = Resolution(
        width=int(res_data.get("width", 640)),
        height=int(res_data.get("height", 480)),
        fps=int(res_data.get("fps", 30)),
    )

    # Parse cameras
    cameras_data = data.get("cameras", {})
    _cameras = [
        Camera(name=name, vid=info["vid"], pid=info["pid"])
        for name, info in cameras_data.items()
    ]

    logger.debug(f"Loaded resolution: {_resolution}")
    logger.debug(f"Loaded cameras: {[c.name for c in _cameras]}")


def get_resolution_config(config_path: Optional[Path] = None) -> Resolution:
    """获取分辨率配置"""
    _load_config(config_path)
    return _resolution  # type: ignore


def get_cameras_config(config_path: Optional[Path] = None) -> list[Camera]:
    """获取摄像头配置"""
    _load_config(config_path)
    return _cameras  # type: ignore

from typing import Optional
from pathlib import Path
import yaml
from dataclasses import dataclass, field
import math

from logger import get_logger
from common.constants import CONFIG_PATH

VEHICLE_CONFIG_PATH = CONFIG_PATH / "vehicle.yaml"

logger = get_logger(__name__)


@dataclass(frozen=True)
class MotionConfig:
    """运动控制参数"""

    max_linear_velocity: float  # m/s
    max_angular_velocity: float  # rad/s

    def __post_init__(self):
        for value in (self.max_linear_velocity, self.max_angular_velocity):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("motion limits must be finite and positive")


@dataclass(frozen=True)
class SerialConfig:
    """串口物理层配置"""

    port: str
    baud_rate: int
    read_timeout: float
    write_timeout: float

    def __post_init__(self):
        if not self.port or self.baud_rate <= 0:
            raise ValueError("serial port and positive baud rate are required")
        for value in (self.read_timeout, self.write_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("serial timeouts must be finite and positive")


@dataclass(frozen=True)
class ProtocolConfig:
    """通信协议配置"""

    # 原始数据是列表，这里定义为 List[int]
    frame_template: list[int] = field(default_factory=list)

    @property
    def template_bytes(self) -> bytes:
        """方便地获取 bytes 对象，不修改原始数据"""
        return bytes(self.frame_template)


@dataclass(frozen=True)
class CommunicationConfig:
    """通信总配置"""

    serial: SerialConfig
    protocol: ProtocolConfig


@dataclass(frozen=True)
class VehicleConfig:
    """
    车辆配置根类
    纯粹的数据容器，不包含控制逻辑
    """

    controller_type: str  # "serial" or "ros2"
    motion: MotionConfig
    communication: CommunicationConfig


_vehicle: Optional[VehicleConfig] = None


def _load_config(config_path: Path):
    global _vehicle
    if _vehicle is not None:
        return

    if config_path is None:
        config_path = VEHICLE_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"vehicle config file not found: {config_path}")
    with open(config_path, encoding="utf8") as f:
        data = yaml.safe_load(f)
    _vehicle = VehicleConfig(
        controller_type=str(data.get("controller", "serial")),
        motion=MotionConfig(
            max_linear_velocity=float(data["motion"]["max_linear_velocity"]),
            max_angular_velocity=float(data["motion"]["max_angular_velocity"]),
        ),
        communication=CommunicationConfig(
            serial=SerialConfig(
                port=str(data["communication"]["serial"]["port"]),
                baud_rate=int(data["communication"]["serial"]["baud_rate"]),
                read_timeout=float(
                    data["communication"]["serial"]["read_timeout"]
                ),
                write_timeout=float(
                    data["communication"]["serial"]["write_timeout"]
                ),
            ),
            protocol=ProtocolConfig(
                frame_template=[
                    int(x) for x in data["communication"]["protocol"]["frame_template"]
                ]
            ),
        ),
    )


def get_vehicle_config(config_path: Optional[Path] = None) -> VehicleConfig:
    _load_config(config_path)
    return _vehicle

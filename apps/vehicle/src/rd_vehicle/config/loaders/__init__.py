# config/loaders/__init__.py
from .media_loader import (
    get_cameras_config,
    Camera,
)
from .server_loader import (
    get_quic_config,
    get_regist_config,
    get_webrtc_config,
    WebRTCConfig,
    QuicConfig,
    RegistConfig,
)
from .vehicle_loader import get_vehicle_config, VehicleConfig

__all__ = [
    # Media
    "get_cameras_config",
    "Camera",
    # Server
    "get_quic_config",
    "get_regist_config",
    "get_webrtc_config",
    "WebRTCConfig",
    "QuicConfig",
    "RegistConfig",
    # Vehicle
    "get_vehicle_config",
    "VehicleConfig",
]

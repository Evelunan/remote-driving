# server_loader.py
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from aiortc import RTCIceServer
from typing import Optional

from rd_vehicle.common.constants import CONFIG_PATH
from rd_core.logging import get_logger


logger = get_logger(__name__)

SERVER_CONFIG_PATH = CONFIG_PATH / "server.yaml"


@dataclass(frozen=True)
class RegistConfig:
    host: str
    port: int
    path: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class SignalingConfig:
    protocol: str
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"


@dataclass(frozen=True)
class WebRTCConfig:
    signaling: SignalingConfig
    ice_servers: list[RTCIceServer] = field(default_factory=list)


@dataclass(frozen=True)
class QuicOptions:
    verify_mode: bool
    enable_datagrams: bool
    max_datagram_frame_size: int
    idle_timeout: int


@dataclass(frozen=True)
class QuicConfig:
    host: str
    port: int
    options: QuicOptions


# 缓存变量（模块级单例）
_regist: RegistConfig | None = None
_webrtc: WebRTCConfig | None = None
_quic: QuicConfig | None = None


def _load_config(config_path: Path):
    global _regist, _webrtc, _quic
    if _regist is not None and _webrtc is not None and _quic is not None:
        return

    if config_path is None:
        config_path = SERVER_CONFIG_PATH

    logger.info(f"Loading server config from {config_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"server config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # --- Regist ---
    reg = data["regist"]
    _regist = RegistConfig(host=reg["host"], port=reg["port"], path=reg["path"])

    # --- Signaling ---
    sig_data = data["webrtc"]["signaling"]
    signaling = SignalingConfig(
        protocol=sig_data["protocol"], host=sig_data["host"], port=sig_data["port"]
    )

    # --- ICE Servers (STUN + TURN) → RTCIceServer ---
    ice_servers: list[RTCIceServer] = []

    ice_data = data["webrtc"]["ice"]

    # Add STUN servers
    for stun in ice_data.get("stun", []):
        ice_servers.append(RTCIceServer(urls=f"stun:{stun['host']}:{stun['port']}"))

    # Add TURN server (if present)
    if "turn" in ice_data:
        turn = ice_data["turn"]
        ice_servers.append(
            RTCIceServer(
                urls=f"turn:{turn['host']}:{turn['port']}",
                username=turn.get("username"),
                credential=turn.get("password"),
            )
        )

    _webrtc = WebRTCConfig(signaling=signaling, ice_servers=ice_servers)

    # --- QUIC ---
    quic_data = data["quic"]
    opts = quic_data["options"]
    _quic = QuicConfig(
        host=quic_data["host"],
        port=quic_data["port"],
        options=QuicOptions(
            verify_mode=opts["verify_mode"],
            enable_datagrams=opts["enable_datagrams"],
            max_datagram_frame_size=opts["max_datagram_frame_size"],
            idle_timeout=opts["idle_timeout"],
        ),
    )


def get_regist_config(config_path: Optional[Path] = None) -> RegistConfig:
    _load_config(config_path)
    return _regist  # type: ignore


def get_webrtc_config(config_path: Optional[Path] = None) -> WebRTCConfig:
    _load_config(config_path)
    return _webrtc  # type: ignore


def get_quic_config(config_path: Optional[Path] = None) -> QuicConfig:
    _load_config(config_path)
    return _quic  # type: ignore

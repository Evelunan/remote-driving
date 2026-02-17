# server_loader.py
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from logger import get_logger
from aiortc import RTCIceServer

logger = get_logger(__name__)

SERVER_CONFIG_PATH = Path.cwd() / "config" / "server.yaml"


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


def _load_config():
    global _regist, _webrtc, _quic
    if _regist is not None and _webrtc is not None and _quic is not None:
        return

    logger.info(f"Loading server config from {SERVER_CONFIG_PATH}")
    if not SERVER_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Media config file not found: {SERVER_CONFIG_PATH}")

    with open(SERVER_CONFIG_PATH, encoding="utf-8") as f:
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


def get_regist_config() -> RegistConfig:
    _load_config()
    return _regist  # type: ignore


def get_webrtc_config() -> WebRTCConfig:
    _load_config()
    return _webrtc  # type: ignore


def get_quic_config() -> QuicConfig:
    _load_config()
    return _quic  # type: ignore

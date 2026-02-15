import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List

from aiortc import RTCIceServer


# ============================================================
# 1️⃣ 读取 YAML（模块加载时执行一次）
# ============================================================

_CONFIG_PATH = Path.cwd() / "config" / "server.yaml"

if not _CONFIG_PATH.exists():
    raise FileNotFoundError(f"Config file not found: {_CONFIG_PATH}")

# ============================================================
# 2️⃣ Regist
# ============================================================

@dataclass(frozen=True)
class RegistConfig:
    host: str
    port: int
    path: str

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

# ============================================================
# WebRTC
# ============================================================

@dataclass(frozen=True)
class SignalingConfig:
    protocol: str
    host: str
    port: int

    @property
    def url(self):
        return f"{self.protocol}://{self.host}:{self.port}"


def _build_ice_servers(ice_dict: dict) -> List[RTCIceServer]:
    servers: List[RTCIceServer] = []

    # STUN
    for stun in ice_dict.get("stun", []):
        servers.append(RTCIceServer(urls=f"stun:{stun['host']}:{stun['port']}"))

    # TURN
    turn = ice_dict.get("turn")
    if turn:
        servers.append(
            RTCIceServer(
                urls=f"turn:{turn['host']}:{turn['port']}",
                username=turn.get("username"),
                credential=turn.get("password"),
            )
        )

    return servers


@dataclass(frozen=True)
class WebRTCConfig:
    signaling: SignalingConfig
    ice_servers: List[RTCIceServer] = field(default_factory=list)



# ============================================================
#   QUIC
# ============================================================


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

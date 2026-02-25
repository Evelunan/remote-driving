from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from logger import get_logger
from common.constants import CONFIG_PATH

logger = get_logger(__name__)
SERVER_CONFIG_FILE = CONFIG_PATH / "server.yaml"


@dataclass(frozen=True)
class QuicConfig:
    host: str
    port: int
    enable_datagrams: bool
    max_datagram_frame_size: int
    idle_timeout: int


_quic_config: Optional[QuicConfig] = None


def get_quic_config(config_path: Optional[Path] = None) -> QuicConfig:
    _load_config(config_path)
    return _quic_config


def _load_config(config_path):
    global _quic_config
    if _quic_config is not None:
        return
    if config_path is None:
        config_path = SERVER_CONFIG_FILE


    logger.info(f"Loading server config from {config_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"server config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
        quic = data["quic"]
        _quic_config = QuicConfig(
            host=str(quic["host"]),
            port=int(quic["port"]),
            enable_datagrams=bool(quic["enable_datagrams"]),
            max_datagram_frame_size=int(quic["max_datagram_frame_size"]),
            idle_timeout=int(quic["idle_timeout"]),
        )

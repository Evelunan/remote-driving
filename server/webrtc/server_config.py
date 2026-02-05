from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from pydantic import Field
from pathlib import Path
from logger import  get_logger

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "server_config.yaml"


class ServerConfig(BaseSettings):
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8765, ge=1, le=65535)

    max_peers_per_room: int = Field(default=2, ge=1)
    ping_interval: float = Field(default=30.0, gt=0)
    ping_timeout: float = Field(default=10.0, gt=0)
    max_message_size: int = Field(default=1_048_576, gt=0)

    model_config = SettingsConfigDict(
        env_prefix="WS_",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=CONFIG_FILE),
            dotenv_settings,
            file_secret_settings,
        )
    
logger = get_logger("webrtc")


if __name__ == "__main__":
    config = ServerConfig()
    logger.info(config.host)
    logger.info(config.port)
    
# app_loader.py
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
import yaml
import math

from rd_core.logging import get_logger
from rd_driving.common.constants import CONFIG_PATH

logger = get_logger(__name__)

APP_CONFIG_PATH = CONFIG_PATH / "app.yaml"


@dataclass(frozen=True)
class AppConfig:
    """应用配置"""

    # 应用信息
    app_name: str
    app_name_en: str
    version: str

    # 调试模式
    debug_mode: bool

    # UI 配置
    window_min_width: int
    window_min_height: int
    theme: str

    # 定时器间隔 (ms)
    frame_timer_interval: int
    stats_timer_interval: int
    time_timer_interval: int
    input_timeout_s: float = 0.2
    control_send_hz: float = 50.0

    def __post_init__(self):
        for value in (self.input_timeout_s, self.control_send_hz):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("control timing must be finite and positive")
        if self.input_timeout_s <= 1 / self.control_send_hz:
            raise ValueError("input timeout must exceed a send period")


_app_config: Optional[AppConfig] = None


def _load_config(config_path: Optional[Path]):
    global _app_config
    if _app_config is not None:
        return

    if config_path is None:
        config_path = APP_CONFIG_PATH

    logger.info(f"Loading app config from {config_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"App config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    ui = data.get("ui", {})
    timers = data.get("timers", {})
    control = data.get("control", {})

    _app_config = AppConfig(
        app_name=str(data.get("app_name", "远程驾驶控制系统")),
        app_name_en=str(data.get("app_name_en", "Remote Driving Control")),
        version=str(data.get("version", "1.0.0")),
        debug_mode=bool(data.get("debug_mode", False)),
        window_min_width=int(ui.get("window_min_width", 1200)),
        window_min_height=int(ui.get("window_min_height", 750)),
        theme=str(ui.get("theme", "modern_glass")),
        frame_timer_interval=int(timers.get("frame_interval", 5)),
        stats_timer_interval=int(timers.get("stats_interval", 1000)),
        time_timer_interval=int(timers.get("time_interval", 1000)),
        input_timeout_s=float(control.get("input_timeout_s", 0.2)),
        control_send_hz=float(control.get("send_hz", 50.0)),
    )

    logger.debug(f"Loaded app config: {_app_config}")


def get_app_config(config_path: Optional[Path] = None) -> AppConfig:
    """获取应用配置"""
    _load_config(config_path)
    return _app_config  # type: ignore

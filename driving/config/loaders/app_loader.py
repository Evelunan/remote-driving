
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """应用配置"""

    # 应用信息
    APP_NAME = "远程驾驶控制系统"
    APP_NAME_EN = "Remote Driving Control"
    VERSION = "1.0.0"

    # UI 配置
    WINDOW_MIN_WIDTH = 1200
    WINDOW_MIN_HEIGHT = 750
    DEFAULT_THEME = "modern_glass"  # 现代毛玻璃主题 dark_tech、modern_glass

    # 视频配置（超低延迟模式）
    VIDEO_QUEUE_SIZE = 1  # 最小队列：只保留最新帧
    VIDEO_FRAME_TIMEOUT = 0.001  # 1ms 超时，最快响应

    # 定时器间隔 (ms)
    FRAME_TIMER_INTERVAL = 5  # ~200fps 轮询，确保最低延迟
    STATS_TIMER_INTERVAL = 500  # 0.5s
    TIME_TIMER_INTERVAL = 1000  # 1s
    VEHICLE_STATUS_INTERVAL = 2000  # 2s 车辆状态轮询间隔
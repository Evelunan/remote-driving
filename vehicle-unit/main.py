# main.py
from application.stream_app import MultiCameraStreamApp

from common.utils import *
from logger import get_logger
from config.loaders import get_webrtc_config, get_quic_config, get_regist_config

logger = get_logger(__name__)


async def start_stream(room: str):
    app = MultiCameraStreamApp()


logger.info(get_regist_config())
logger.info(get_webrtc_config())
logger.info(get_quic_config())

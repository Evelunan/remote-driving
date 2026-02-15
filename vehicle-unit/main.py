# main.py


from common.utils import *
from logger import get_logger
from models.config_models.server_config import WEBRTC_CONFIG
logger = get_logger(__name__)


logger.info(WEBRTC_CONFIG.ice_servers)


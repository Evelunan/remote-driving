# log_utils.py
import logging
import logging.config
import os
import yaml

from common import CONFIG_PATH

LOG_CONFIG_FILE = CONFIG_PATH / "log.yaml"
_LOGGER_SETUP_DONE = False


def setup_logger(config_path=LOG_CONFIG_FILE):
    """
    使用 YAML 配置初始化日志系统。
    支持动态日志目录和文件名。
    幂等：多次调用无副作用。
    """
    global _LOGGER_SETUP_DONE
    if _LOGGER_SETUP_DONE:
        return

    if not os.path.exists(config_path):
        # 可选：若配置不存在，可回退到默认配置，或报错
        raise FileNotFoundError(f"日志配置文件未找到: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    filename = config["handlers"]["file"]["filename"]
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    logging.config.dictConfig(config)
    _LOGGER_SETUP_DONE = True

    # 可选：记录初始化成功（但注意此时 logger 已可用）
    logger = logging.getLogger(__name__)
    logger.debug("✅ 日志系统已通过 YAML 配置初始化")
    logger.debug(f"日志文件: {filename}")


def get_logger(name: str):
    """
    获取命名 logger，**自动确保日志系统已初始化**。
    首次调用时会加载 YAML 配置。
    """
    global _LOGGER_SETUP_DONE
    if not _LOGGER_SETUP_DONE:
        setup_logger()  # 自动初始化
    return logging.getLogger(name)

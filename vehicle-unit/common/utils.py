# utils.py

import ctypes
import platform
from pathlib import Path
from functools import lru_cache
from typing import Optional

from logger import get_logger

ROOT_PATH = Path.cwd()
logger = get_logger(__name__)

SYSTEM = platform.system()


# ==========================
# Linux 依赖预加载
# ==========================
def _preload_linux_dependencies():
    libs = ["libudev.so.1", "libusb-1.0.so.0"]
    for lib in libs:
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            logger.debug(f"预加载依赖失败 {lib}: {e}")


# ==========================
# 加载主动态库（只加载一次）
# ==========================
@lru_cache(maxsize=1)
def _load_camera_lib() -> Optional[ctypes.CDLL]:
    if SYSTEM == "Windows":
        lib_filename = "CvCameraIndex_x64.dll"
    elif SYSTEM == "Linux":
        lib_filename = "libcamera_index.so"
        _preload_linux_dependencies()
    else:
        logger.error(f"Unsupported operating system: {SYSTEM}")
        return None

    lib_path = ROOT_PATH / "lib" / lib_filename

    if not lib_path.is_file():
        logger.error(f"Camera index library not found: {lib_path}")
        return None

    try:
        lib = ctypes.CDLL(str(lib_path))

        lib.getCameraIndex.argtypes = [ctypes.c_char_p]
        lib.getCameraIndex.restype = ctypes.c_int

        logger.info(f"Camera index library loaded: {lib_path}")
        return lib

    except OSError as e:
        logger.error(f"动态库加载失败: {e}")
        return None


# ==========================
# 生成硬件ID
# ==========================
def _get_camera_hwid(vid: str, pid: str) -> Optional[str]:
    if not vid or not pid:
        logger.error("VID or PID is empty")
        return None

    vid = str(vid).strip()
    pid = str(pid).strip()

    if SYSTEM == "Windows":
        return f"VID_{vid.upper()}&PID_{pid.upper()}"
    elif SYSTEM == "Linux":
        return f"{vid.lower()}:{pid.lower()}"
    else:
        logger.error(f"Unsupported operating system: {SYSTEM}")
        return None


# ==========================
# 对外接口
# ==========================
def get_camera_index(vid: str, pid: str) -> int:
    """
    根据 VID / PID 获取摄像头索引

    返回值：
        >=0  : 成功
        -1   : 未找到设备
        -2   : 参数错误
        -3   : 库加载失败
    """

    lib = _load_camera_lib()
    if lib is None:
        return -3

    hwid = _get_camera_hwid(vid, pid)
    if hwid is None:
        return -2

    try:
        return lib.getCameraIndex(hwid.encode("utf-8"))

    except Exception as e:
        logger.exception(f"获取摄像头索引异常: {e}")
        return -3

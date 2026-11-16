"""Import optional OpenCV without replacing the host Qt plugin/font settings."""

import importlib
import os

# The Linux opencv-python wheel exports its own Qt paths at import time. Those
# plugins belong to OpenCV's Qt build, not to the application's PyQt6 runtime.
_qt_keys = ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR")
_prior = {key: os.environ.get(key) for key in _qt_keys}
try:
    cv2 = importlib.import_module("cv2")
finally:
    for _key, _value in _prior.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value

__all__ = ["cv2"]

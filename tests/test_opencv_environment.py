import pytest


@pytest.mark.parametrize("configured", [False, True])
def test_native_opencv_preserves_qt_environment(legacy_python, configured):
    legacy_python(
        "driving",
        f"""
        import os
        keys = ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR")
        for key in keys:
            if {configured!r}:
                os.environ[key] = "/tmp/user-selected-" + key.lower()
            else:
                os.environ.pop(key, None)
        before = {{key: os.environ.get(key) for key in keys}}
        from rd_webrtc.opencv import cv2
        import numpy as np
        assert before == {{key: os.environ.get(key) for key in keys}}
        pixels = np.zeros((24, 32, 3), dtype=np.uint8)
        assert cv2.resize(pixels, (64, 48)).shape == (48, 64, 3)
    """,
    )

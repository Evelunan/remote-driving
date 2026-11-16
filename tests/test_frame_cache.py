import threading
from dataclasses import FrozenInstanceError

import numpy as np
import pytest
from rd_driving.infrastructure.video.frame_buffer import FrameBuffer


def test_published_snapshot_is_owned_and_stable():
    cache = FrameBuffer()
    source = np.full((7, 11, 3), 37, dtype=np.uint8)
    cache.put("front", source, received_at=10)
    old = cache.get("front")
    source[:] = 91
    assert np.all(old.frame == 37)
    assert cache.get("front") is old
    assert cache.all_frames[0] is old
    assert cache.get_if_new("front", old.id) is None
    with pytest.raises(ValueError):
        old.frame[:] = 0
    with pytest.raises(ValueError):
        old.frame.flags.writeable = True
    with pytest.raises(FrozenInstanceError):
        old.id = 2
    cache.put("front", source, received_at=11)
    new = cache.get_if_new("front", old.id)
    assert new.id > old.id and np.all(new.frame == 91)
    assert np.all(old.frame == 37)
    assert old.fresh(0.5, now=10.499)
    assert not old.fresh(0.5, now=10.5)
    assert not old.fresh(0.5, now=9.99)


def test_session_clear_rejects_late_callback_including_copy_in_progress():
    cache = FrameBuffer()
    generation = cache.begin_session()
    copying, resume = threading.Event(), threading.Event()

    class SlowFrame(np.ndarray):
        def copy(self, order="C"):
            copying.set()
            assert resume.wait(2)
            return super().copy(order=order)

    frame = np.ones((2, 2, 3), dtype=np.uint8).view(SlowFrame)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(cache.put("front", frame, generation=generation))
    )
    worker.start()
    try:
        assert copying.wait(2)
        # Clearing metadata must not wait for a pixel copy under the lock.
        cache.clear()
        assert cache.get("front").frame is None
    finally:
        resume.set()
        worker.join(2)
    assert not worker.is_alive() and results == [False]
    assert not cache.put("front", frame, generation=generation)
    assert cache.get("front").frame is None


@pytest.mark.parametrize(
    "source",
    [
        np.zeros((2, 2)),
        np.zeros((0, 2, 3), dtype=np.uint8),
        np.zeros((2, 2, 4), dtype=np.uint8),
    ],
)
def test_invalid_images_are_rejected(source):
    with pytest.raises(ValueError):
        FrameBuffer().put("front", source)


def test_qt_readonly_frames_expire_and_recover(legacy_python):
    legacy_python(
        "driving",
        r"""
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        import time
        import numpy as np
        from PyQt6.QtWidgets import QApplication
        from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
        from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
        app = QApplication([])
        cache = FrameBuffer()
        widget = MultiVideoWidget(cache, stale_frame_ms=100, debug_overlay=True)
        display = widget.get_display("front")
        source = np.full((180, 320, 3), 53, dtype=np.uint8)
        cache.put("front", source)
        widget.update()
        assert display.get_resolution() == (320, 180)
        assert not display.pixmap().isNull()
        assert np.all(cache.get("front").frame == 53)
        unique_count = display._frame_count
        pixmap_key = display.pixmap().cacheKey()
        widget.update()
        assert display._frame_count == unique_count
        assert display.pixmap().cacheKey() == pixmap_key
        cache.put("front", source, received_at=time.monotonic() - 1)
        widget.update()
        assert "已过期" in display.text()
        assert display.get_fps() == 0 and display.get_resolution() == (0, 0)
        assert display.pixmap().isNull()
        cache.put("front", source)
        widget.update()
        assert display.get_resolution() == (320, 180)
        assert not display.pixmap().isNull()
        assert display._scaled_buffer is not None
        cache.clear()
        widget.update()
        assert "等待" in display.text() and display._current_frame is None
        widget.close()
    """,
    )


def test_opengl_consumer_contract_without_gpu(legacy_python):
    legacy_python(
        "driving",
        r"""
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from types import SimpleNamespace
        from unittest.mock import Mock
        import numpy as np
        import time
        from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
        from rd_driving.ui.widgets.gl_multi_video_widget import GLMultiVideoWidget, GL, CAMERA_NAMES
        cache = FrameBuffer()
        widget = SimpleNamespace(
            fb=cache, _stale_after_s=0.5, _frame_ids={}, _texture_sizes={},
            textures={name: index for index, name in enumerate(CAMERA_NAMES)},
            meta={name: {} for name in CAMERA_NAMES}, _fps_counter={},
            width=lambda: 600, height=lambda: 400,
            _get_layout_rects=lambda w, h: {name: (0, 0, w, h) for name in CAMERA_NAMES},
            _calc_rect=lambda vw, vh, w, h: (0, 0, w, h),
            _to_gl=lambda *args: args[:4], _draw=Mock(), _draw_overlay=Mock(), _update_fps=Mock(),
        )
        widget._update_texture = lambda name, frame: GLMultiVideoWidget._update_texture(widget, name, frame)
        for name in ("glClearColor", "glClear", "glBindTexture", "glPixelStorei", "glTexImage2D", "glTexSubImage2D"):
            setattr(GL, name, Mock())
        source = np.zeros((7, 11, 3), dtype=np.uint8)
        cache.put("front", source)
        GLMultiVideoWidget.paintGL(widget)
        GLMultiVideoWidget.paintGL(widget)
        assert GL.glTexImage2D.call_count == 1
        assert GL.glTexSubImage2D.call_count == 0
        assert widget._update_fps.call_count == 1
        args = GL.glTexImage2D.call_args.args
        assert args[6] == GL.GL_BGR and args[-1] is cache.get("front").frame
        GL.glPixelStorei.assert_called_with(GL.GL_UNPACK_ALIGNMENT, 1)
        cache.put("front", source)
        GLMultiVideoWidget.paintGL(widget)
        assert GL.glTexSubImage2D.call_count == 1
        cache.put("front", np.zeros((9, 13, 3), dtype=np.uint8))
        GLMultiVideoWidget.paintGL(widget)
        assert GL.glTexImage2D.call_count == 2
        drawn = widget._draw.call_count
        cache.put("front", source, received_at=time.monotonic() - 1)
        GLMultiVideoWidget.paintGL(widget)
        assert widget._draw.call_count == drawn
        assert widget.meta["front"] == {"size": None, "fps": 0}
    """,
    )

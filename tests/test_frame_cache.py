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


def test_publications_coalesce_and_old_consumers_cannot_claim_new_notification():
    cache = FrameBuffer()
    notices = []

    def old():
        notices.append("old")

    def new():
        notices.append("new")

    cache.set_notifier(old)
    pixels = np.zeros((2, 2, 3), np.uint8)
    for view in cache.positions:
        cache.put(view, pixels)
    assert notices == ["old"]
    cache.set_notifier(new)
    cache.clear()
    cache.remove_notifier(old)
    assert not cache.consume_notification(old)
    assert cache.consume_notification(new)
    cache.put("front", pixels)
    assert notices == ["old", "new", "new"]


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
        from unittest.mock import patch
        import numpy as np
        from PyQt6.QtWidgets import QApplication
        from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
        from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
        app = QApplication([])
        cache = FrameBuffer()
        widget = MultiVideoWidget(cache, stale_frame_ms=100, debug_overlay=True)
        display = widget.get_display("front")
        widget.show()
        app.processEvents()
        assert display.get_latency_ms() is None
        source = np.full((180, 320, 3), 53, dtype=np.uint8)
        cache.put("front", source, captured_at=time.monotonic() - 0.12)
        app.processEvents()  # Producer notification delivers the frame without polling.
        app.processEvents()
        latency = display.get_latency_ms()
        assert latency is not None and latency >= 119
        assert widget.get_display("back").get_latency_ms() is None
        with patch("time.monotonic", return_value=time.monotonic() + 0.02):
            assert display.get_latency_ms() > latency
        assert display.get_resolution() == (320, 180)
        def center_pixel():
            return display.grab().toImage().pixelColor(display.rect().center()).red()
        assert center_pixel() == 53
        widget.resize(1280, 480)
        app.processEvents()
        assert center_pixel() == 53
        assert display.grab().toImage().pixelColor(0, 0) == display._background
        assert np.all(cache.get("front").frame == 53)
        unique_count = display._frame_count
        image_key = display._image.cacheKey()
        widget.update()
        assert display._frame_count == unique_count
        assert display._image.cacheKey() == image_key
        display._last_fps_time = time.monotonic()
        display._frame_count = 0
        for _ in range(3):
            cache.put("front", source)
            widget.update()
        assert display._frame_count == 0
        app.processEvents()
        assert display._frame_count == 1
        display.repaint()
        assert display._frame_count == 1
        cache.put("front", source, received_at=time.monotonic() - 1)
        widget.check_expiry()
        assert "已过期" in display.accessibleDescription()
        assert display.get_fps() == 0 and display.get_resolution() == (0, 0)
        assert display.get_latency_ms() is None
        assert display._image.isNull() and center_pixel() != 53
        cache.put("front", source)
        widget.update()
        assert display.get_resolution() == (320, 180)
        assert center_pixel() == 53
        assert display._current_frame is cache.get("front").frame
        cache.clear()
        widget.update()
        assert "等待" in display.accessibleDescription() and display._current_frame is None
        widget.close()
    """,
    )


def test_layout_switch_preserves_displays_and_presentation_evidence(legacy_python):
    legacy_python(
        "driving",
        r"""
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        import time
        from unittest.mock import Mock
        import numpy as np
        from PyQt6.QtWidgets import QApplication, QLabel
        from rd_core.video_safety import VideoSource
        from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
        from rd_driving.ui.themes import ModernGlassTheme
        from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
        app = QApplication([])
        cache, gate = FrameBuffer(), Mock()
        widget = MultiVideoWidget(
            cache, video_gate=gate, stale_frame_ms=5000, primary_view="right"
        )
        widget.apply_theme(ModernGlassTheme())
        widget.resize(800, 640)
        widget.show()
        app.processEvents()
        displays = {name: widget.get_display(name) for name in cache.positions}
        parents = {name: panel.parentWidget() for name, panel in displays.items()}
        primary = parents["right"]
        assert widget.size().width() == 800
        assert primary.y() < parents["left"].y()
        assert parents["left"].x() < parents["back"].x() < parents["front"].x()
        assert 0.7 < primary.height() / widget.height() < 0.8
        assert all(widget.rect().contains(tile.geometry()) for tile in parents.values())
        for panel in displays.values():
            assert "等待" in panel.accessibleDescription()
        pixels = np.zeros((180, 320, 3), np.uint8)
        sources = {name: VideoSource("vehicle", 1, name) for name in displays}
        for name in displays:
            cache.put(name, pixels, source=sources[name])
        app.processEvents()
        app.processEvents()
        widget.resize(1190, 780)
        app.processEvents()
        assert gate.observe.call_count == 1
        for index, mode in enumerate(("grid", "front_dominant") * 3, start=1):
            widget.set_layout(mode)
            app.processEvents()
            assert gate.observe.call_count == index
            for name, panel in displays.items():
                assert panel is widget.get_display(name) and panel.isVisible()
                label = {"front": "前视", "back": "后视", "left": "左视", "right": "右视"}[name]
                assert panel.accessibleName() == label and not panel.accessibleDescription()
                assert panel.parentWidget() is parents[name]
                title = parents[name].findChild(QLabel, "cameraTitle")
                assert title.text() == label and title.geometry().bottom() < panel.y()
                marker = parents[name].findChild(QLabel, "primaryCameraMarker")
                assert (marker is not None) == (name == "right")
                if marker is not None:
                    assert marker.text() == "主画面" and marker.geometry().bottom() < panel.y()
                assert panel._last_frame_id == index
                cache.put(name, pixels, source=sources[name])
            assert (primary.width() > 1.5 * parents["left"].width()) == (mode == "front_dominant")
            app.processEvents()
            app.processEvents()
            assert gate.observe.call_count == index + 1
            assert gate.observe.call_args.args[2] == sources["right"]
        cache.put("right", pixels, received_at=time.monotonic() - 10)
        widget.check_expiry()
        assert "已过期" in displays["right"].accessibleDescription()
        assert displays["right"]._image.isNull()
        assert displays["front"].isVisible() and not displays["front"]._image.isNull()
        gate.invalidate.assert_called_once_with("picture_unavailable")
        widget.close()
    """,
    )


def test_publication_during_gui_cache_read_is_fresh(legacy_python):
    legacy_python(
        "driving",
        r"""
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from unittest.mock import patch
        import numpy as np
        from PyQt6.QtWidgets import QApplication
        from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
        from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget

        app = QApplication([])
        cache = FrameBuffer()
        widget = MultiVideoWidget(cache)
        original = cache.get
        now = 10.0
        def read(name):
            global now
            if name == "front":
                now += 0.01
                cache.put(name, np.zeros((2, 3, 3), np.uint8), received_at=now)
            return original(name)
        with patch.object(cache, "get", read), patch("time.monotonic", lambda: now):
            widget.update()
            assert widget.get_display("front").get_resolution() == (3, 2)
            widget.check_expiry()
            assert widget.get_display("front").get_resolution() == (3, 2)
    """,
    )

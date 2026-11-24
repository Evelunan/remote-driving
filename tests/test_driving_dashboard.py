import pytest


@pytest.mark.parametrize("theme", ["modern_glass", "dark_tech"])
def test_dashboard_fits_four_cameras_and_key_metrics(legacy_python, theme):
    legacy_python(
        "driving",
        f"""
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QApplication
        from rd_core.settings import DrivingSettings, UISettings, VideoSafetySettings
        from rd_driving.main_window import MainWindow

        app = QApplication([])
        room = "vehicle-room-" * 9
        settings = DrivingSettings(
            room=room,
            session=0,
            ui=UISettings(theme={theme!r}),
            video=VideoSafetySettings(primary_view="right"),
        )
        window = MainWindow(settings=settings)
        window._session_id, window._room = settings.session, settings.room
        window._setup_control_page()
        window._stacked_widget.setCurrentWidget(window._control_page)
        window.show()
        navbar = window._navbar
        panel = window._control_panel
        video = window._multi_video_widget
        for width, height in ((1200, 750), (1920, 1080), (1200, 750)):
            window.resize(width, height)
            app.processEvents()
            assert (window.width(), window.height()) == (width, height)
            assert navbar._session_text.text() == "0"
            assert navbar._room_text.accessibleName() == room
            assert "…" in navbar._room_text.text()
            assert not navbar._actions["退出会话"].visibleRegion().isEmpty()
            main = video.get_display("right")
            auxiliaries = [video.get_display(name) for name in ("left", "back", "front")]
            assert all(main.height() > 2 * display.height() for display in auxiliaries)
            assert [display.mapTo(video, QPoint()).x() for display in auxiliaries] == sorted(
                display.mapTo(video, QPoint()).x() for display in auxiliaries
            )
            assert all(not display.visibleRegion().isEmpty() for display in [main, *auxiliaries])
            for item in (
                panel._gear, *panel._input_stats.values(),
                panel._vehicle_stats["speed_mps"], panel._confirmation_state,
            ):
                assert not item.visibleRegion().isEmpty()
        window.close()
        app.processEvents()
        """,
    )


def test_dashboard_control_status_tracks_worker_and_clears_on_logout(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtWidgets import QApplication
        from rd_driving.infrastructure.workers.control_system_thread import STATUS_TEXT
        from rd_driving.main_window import MainWindow

        class Worker:
            running = True
            stop_requested = False

            def isRunning(self):
                return self.running

            def request_stop(self):
                self.stop_requested = True

        app = QApplication([])
        window = MainWindow()
        window._session_id, window._room = 42, "test-room"
        window._setup_control_page()
        window._stacked_widget.setCurrentWidget(window._control_page)
        window._setup_signals()
        window._services_running = True
        worker = window._control_thread = Worker()
        navbar = window._navbar
        channel = window._control_panel.get_connection_items()["quic"]
        signal = window._bridge.quic_status_changed
        for state, online, tone in (
            ("registered", True, "waiting"),
            ("waiting_input", True, "waiting"),
            ("waiting_neutral", True, "waiting"),
            ("waiting_execution", True, "waiting"),
            ("active", True, "active"),
            ("video_unavailable", False, "blocked"),
            ("active", True, "active"),
        ):
            text = STATUS_TEXT[state]
            signal.emit(online, text)
            assert navbar._control_status.text() == text
            assert navbar._control_tone == tone
            assert (window._theme.success in navbar._control_field.styleSheet()) == (tone == "active")
            assert channel.is_online() == online
            assert channel._status_label.text() == (STATUS_TEXT["registered"] if online else text)

        login = window._login_page
        login._room_input.setText("saved-room")
        login._show_error("旧提示")
        navbar._actions["退出会话"].click()
        assert worker.stop_requested
        assert navbar._control_status.text() == "正在退出会话"
        signal.emit(True, STATUS_TEXT["active"])
        assert navbar._control_status.text() == "正在退出会话"
        worker.running = False
        window._finish_shutdown()
        assert window._stacked_widget.currentWidget() is login
        assert login._room_input.text() == "saved-room"
        assert not login._status_label.text()
        window.close()
        app.processEvents()
        """,
    )


def test_dashboard_camera_stats_follow_each_painted_view_and_clear_on_loss(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        import time
        from unittest.mock import patch
        import numpy as np
        from PyQt6.QtWidgets import QApplication, QLabel
        from rd_driving.main_window import MainWindow

        app = QApplication([])
        window = MainWindow()
        window._session_id, window._room = 42, "test-room"
        window._setup_control_page()
        window._services_running = True
        window._stacked_widget.setCurrentWidget(window._control_page)
        window.show()
        app.processEvents()

        video = window._multi_video_widget
        displays = {view: video.get_display(view) for view in window.fb.positions}
        metrics = {
            view: display.parentWidget().findChild(QLabel, "cameraMetrics")
            for view, display in displays.items()
        }
        assert all(label is not None and not label.visibleRegion().isEmpty()
                   for label in metrics.values())

        def text(view):
            return metrics[view].text().split()

        unavailable = "-- FPS 延迟 -- ms".split()
        window._update_camera_info()
        assert all(text(view) == unavailable for view in displays)

        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        now = time.monotonic()
        measurements = {
            "front": (24, 83), "left": (18, 154), "right": (16, None), "back": (12, 227),
        }
        with patch("time.monotonic", return_value=now):
            for view, (_, latency) in measurements.items():
                captured_at = now - latency / 1000 if latency is not None else None
                window.fb.put(view, frame, received_at=now, captured_at=captured_at)
            app.processEvents()
            app.processEvents()
            for view, (fps, _) in measurements.items():
                displays[view]._fps = fps
            window._update_camera_info()
            assert text("front") == "24 FPS 延迟 83 ms".split()
            assert text("left") == "18 FPS 延迟 154 ms".split()
            assert text("right") == "16 FPS 延迟 -- ms".split()
            assert text("back") == "12 FPS 延迟 227 ms".split()

            previous = now - window.settings.video.stale_frame_ms / 1000 - 1
            window.fb.put("front", frame, received_at=previous)
            video.check_expiry()
            assert text("front") == unavailable
            assert text("left") == "18 FPS 延迟 154 ms".split()
            video.show_message_all("等待视频流...")
            assert all(text(view) == unavailable for view in displays)

        window.close()
        app.processEvents()
        """,
    )

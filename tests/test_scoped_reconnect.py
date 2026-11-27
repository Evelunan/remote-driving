import asyncio

import pytest
from rd_core.settings import DrivingSettings
from rd_driving.app.input_service import DrivingInputService


@pytest.mark.asyncio
@pytest.mark.parametrize("close_during_cleanup", [False, True])
async def test_quic_restart_waits_for_cleanup_and_keeps_usb(close_during_cleanup):
    started, cleaning, release, restarted = [asyncio.Event() for _ in range(4)]

    class Device:
        attempts = 0
        closed = False

        def set_data_callback(self, callback):
            pass

        async def connect(self):
            self.attempts += 1

        def is_connected(self):
            return True

        async def disconnect(self):
            self.closed = True

    class Client:
        runs = 0
        cleaned = 0

        async def run(self):
            self.runs += 1
            (started if self.runs == 1 else restarted).set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                self.cleaned += 1

    device, client = Device(), Client()
    service = DrivingInputService(
        DrivingSettings(), device_factory=lambda *args: device
    )
    service.client = client
    task = asyncio.create_task(service.run())
    cancel_requested = False
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert service.request_control_reconnect()
        assert not service.request_control_reconnect()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert device.attempts == 1 and not device.closed
        assert client.runs == 1
        if close_during_cleanup:
            cancel_requested = task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        release.set()
        if not close_during_cleanup:
            await asyncio.wait_for(restarted.wait(), 1)
            assert client.cleaned == 1 and client.runs == 2
            assert device.attempts == 1 and not device.closed
    finally:
        release.set()
        if not cancel_requested:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert device.closed
    assert client.cleaned == client.runs
    assert not service.request_control_reconnect()


@pytest.mark.parametrize("close_during_restart", [False, True])
def test_media_row_actions_keep_control_worker_and_ignore_old_signals(
    legacy_python, close_during_restart
):
    legacy_python(
        "driving",
        f"""
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        import time, threading
        import numpy as np
        from PyQt6.QtCore import QThread, pyqtSignal
        from PyQt6.QtWidgets import QApplication
        import rd_driving.main_window as module
        app = QApplication([])
        workers = []
        class Worker(QThread):
            connection_changed = pyqtSignal(bool)
            status_changed = pyqtSignal(str, str)
            error_occurred = pyqtSignal(str)
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.args = args
                self.stop = threading.Event()
                self.control_requests = 0
                workers.append(self)
            def run(self):
                self.stop.wait(3)
                time.sleep(0.10)
            def request_stop(self):
                self.stop.set()
            def request_control_reconnect(self):
                self.control_requests += 1
                return True
        module.ControlSystemThread = module.WebRTCReceiverThread = Worker
        window = module.MainWindow(session_id=42, room='video')
        window.show()
        panel = window._control_panel
        old_control = window._control_thread
        assert window._receiver_thread is None
        assert set(panel._action_buttons) == {{'signal', 'webrtc', 'usb', 'quic', 'video', 'control'}}
        assert panel._action_buttons['video'].text() == '连接视频'
        assert not panel._action_buttons['signal'].isEnabled()
        def until(predicate):
            deadline = time.monotonic() + 3
            while not predicate():
                assert time.monotonic() < deadline
                app.processEvents()
                time.sleep(0.005)
        try:
            panel.set_connection_status('quic', True, '等待控制设备')
            panel._action_buttons['quic'].click()
            assert old_control.control_requests == 1
            panel._action_buttons['video'].click()
            old_video = window._receiver_thread
            old_video.status_changed.emit('joined', '')
            app.processEvents()
            assert panel._action_buttons['video'].text() == '断开视频'
            assert not old_video.stop.is_set() and not old_control.stop.is_set()
            panel._action_buttons['signal'].click()
            panel._action_buttons['webrtc'].click()
            assert window._media_restart_pending
            assert not panel._action_buttons['signal'].isEnabled()
            assert not panel._action_buttons['webrtc'].isEnabled()
            assert old_video.stop.is_set() and not old_control.stop.is_set()
            assert not window.video_gate.snapshot().ready
            if {close_during_restart!r}:
                window.close()
                until(lambda: window._closing_ready)
                assert len(workers) == 2
            else:
                until(lambda: window._receiver_thread is not old_video)
                assert not old_video.isRunning()
                assert window._control_thread is old_control and not old_control.stop.is_set()
                old_video.status_changed.emit('connected', '')
                old_video.args[0]._on_frame('front', np.ones((24,32,3), dtype=np.uint8), None)
                app.processEvents()
                assert not window._webrtc_connected
                assert window.fb.get('front').frame is None
                window._receiver_thread.status_changed.emit('joined', '')
                app.processEvents()
                assert panel._action_buttons['signal'].isEnabled()
                assert panel._action_buttons['webrtc'].isEnabled()
        finally:
            window.close()
            until(lambda: window._closing_ready)
            app.processEvents()
        assert all(not worker.isRunning() for worker in workers)
    """,
    )

import os
import subprocess
import sys
from textwrap import dedent

import pytest


@pytest.mark.parametrize("number", ["SIGTERM", "SIGINT"])
@pytest.mark.parametrize("connected", [False, True])
def test_formal_gui_signals_wait_for_workers_and_restore_handlers(
    legacy_python, number, connected
):
    legacy_python(
        "driving",
        f"""
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        import signal
        import threading
        import time
        from PyQt6.QtCore import QThread, QTimer, pyqtSignal
        from rd_core.settings import DrivingSettings
        import rd_driving.main_window as module

        workers, windows, ticks, finished = [], [], [], set()
        class Worker(QThread):
            connection_changed = pyqtSignal(bool)
            status_changed = pyqtSignal(str, str)
            error_occurred = pyqtSignal(str)
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.stopping = threading.Event()
                self.requests = 0
                workers.append(self)
                self.finished.connect(lambda: finished.add(id(self)))
            def run(self):
                assert self.stopping.wait(3)
                time.sleep(0.20)
            def request_stop(self):
                self.requests += 1
                self.stopping.set()
        module.ControlSystemThread = Worker
        module.WebRTCReceiverThread = Worker
        original = module.MainWindow

        def request_shutdown():
            # os.kill with these signals would terminate a Windows process.
            signal.raise_signal(signal.{number})

        class ObservedWindow(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                windows.append(self)
                self.tick = QTimer(self)
                self.tick.timeout.connect(lambda: ticks.append(time.monotonic()))
                self.tick.start(5)
                QTimer.singleShot(25, request_shutdown)
                if {connected!r}:
                    QTimer.singleShot(85, request_shutdown)
                    QTimer.singleShot(135, request_shutdown)
        module.MainWindow = ObservedWindow
        prior = {{n: signal.getsignal(n) for n in (signal.SIGINT,signal.SIGTERM)}}
        assert module.run_gui(DrivingSettings(), connect={connected!r}) == 0
        assert windows[0]._closing_ready
        assert all(id(worker) in finished and worker.requests == 1 for worker in workers)
        assert len(workers) == (2 if {connected!r} else 0)
        assert len(ticks) >= (10 if {connected!r} else 1)
        assert all(signal.getsignal(n) == handler for n,handler in prior.items())
    """,
    )


@pytest.mark.parametrize("via_signal", [False, True], ids=["window-close", "sigint"])
def test_gui_exits_during_webrtc_peer_removal(legacy_python, via_signal):
    legacy_python(
        "driving",
        f"""
        import asyncio
        import os
        import signal
        import threading
        from unittest.mock import AsyncMock

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtCore import QTimer
        from rd_core.settings import DrivingSettings
        import rd_driving.main_window as module

        closing_peer = threading.Event()
        peers = []
        windows = []

        class Receiver(module.WebRTCReceiver):
            async def start(self):
                self.finish_peer_stop = asyncio.Event()
                self.signaling.connect = AsyncMock()
                self.signaling.join = AsyncMock(return_value={{"id": "driver"}})
                self.signaling.listen = self._listen_for_test
                await super().start()

            async def _listen_for_test(self):
                peer = await self._create_peer("vehicle")
                peers.append(peer)
                transceiver = peer.pc.addTransceiver("video")
                stop = transceiver.stop

                async def paused_stop():
                    closing_peer.set()
                    await self.finish_peer_stop.wait()
                    await stop()

                transceiver.stop = paused_stop
                await self._on_peer_left({{"id": "vehicle"}})

            async def _disconnect(self):
                self.finish_peer_stop.set()
                await super()._disconnect()

        class Window(module.MainWindow):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                windows.append(self)
                self.close_when_ready = QTimer(self)
                self.close_when_ready.timeout.connect(self._close_during_peer_removal)
                self.close_when_ready.start(10)

            def _start_control(self):
                pass

            def _close_during_peer_removal(self):
                if closing_peer.is_set():
                    self.close_when_ready.stop()
                    if {via_signal!r}:
                        signal.raise_signal(signal.SIGINT)
                    else:
                        self.close()

        module.WebRTCReceiver = Receiver
        module.MainWindow = Window
        assert module.run_gui(DrivingSettings(), connect=True) == 0
        assert windows[0]._closing_ready
        assert windows[0]._receiver_thread is None
        assert len(peers) == 1 and peers[0].pc.connectionState == "closed"
        """,
    )


def test_signal_handlers_are_restored_when_gui_host_raises(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        import signal
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QWidget
        from rd_driving.shutdown import shutdown_signals
        app=QApplication([])
        window=QWidget()
        prior={n:signal.getsignal(n) for n in (signal.SIGINT,signal.SIGTERM)}
        try:
            with shutdown_signals(window):
                raise RuntimeError('host failed')
        except RuntimeError:
            pass
        assert all(signal.getsignal(n)==handler for n,handler in prior.items())
        assert all(not timer.isActive() for timer in window.findChildren(QTimer))
    """,
    )


@pytest.mark.parametrize("via_signal", [False, True], ids=["window-close", "sigint"])
def test_gui_forces_process_exit_when_worker_never_stops(tmp_path, via_signal):
    script = f"""
        import signal
        import threading
        import time

        from PyQt6.QtCore import QThread, QTimer
        from rd_core.settings import DrivingSettings, VideoSafetySettings
        from rd_core.video_safety import VideoSource
        import rd_driving.main_window as module
        import rd_driving.shutdown as shutdown

        shutdown.SHUTDOWN_GRACE_S = 0.4

        class StalledWorker(QThread):
            def __init__(self, bridge, settings, *, video_gate):
                super().__init__()
                self.video_gate = video_gate

            def run(self):
                threading.Event().wait()

            def request_stop(self):
                permit = self.video_gate.snapshot()
                assert not permit.ready and permit.reason == "service_stopped"
                print("worker stop requested after picture revocation", flush=True)

        class Window(module.MainWindow):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.repeat_close = QTimer(self)
                self.repeat_close.timeout.connect(self._close)
                QTimer.singleShot(0, self._start_closing)

            def _start_video(self):
                pass

            def _start_closing(self):
                now = time.monotonic()
                source = VideoSource("vehicle", self.settings.session, "peer")
                previous = now - 0.01
                assert self.video_gate.observe(
                    1, previous, source, captured_at=previous, now=previous
                )
                assert self.video_gate.observe(2, now, source, captured_at=now)
                assert self.video_gate.snapshot().ready
                assert self._control_thread.isRunning()
                self.repeat_close.start(30)
                self._close()

            def _close(self):
                if {via_signal!r}:
                    signal.raise_signal(signal.SIGINT)
                else:
                    self.close()

        module.ControlSystemThread = StalledWorker
        module.MainWindow = Window
        settings = DrivingSettings(video=VideoSafetySettings(recovery_ms=1))
        raise SystemExit(module.run_gui(settings, connect=True))
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", dedent(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert result.stdout.count("worker stop requested after picture revocation") == 1, output
    assert "Timeout" in result.stderr, output

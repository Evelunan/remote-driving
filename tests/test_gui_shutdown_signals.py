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
        class ObservedWindow(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                windows.append(self)
                self.tick = QTimer(self)
                self.tick.timeout.connect(lambda: ticks.append(time.monotonic()))
                self.tick.start(5)
                QTimer.singleShot(25, lambda: os.kill(os.getpid(), signal.{number}))
                if {connected!r}:
                    QTimer.singleShot(85, lambda: os.kill(os.getpid(), signal.{number}))
                    QTimer.singleShot(135, lambda: os.kill(os.getpid(), signal.{number}))
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

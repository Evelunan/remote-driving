def test_control_worker_delivers_state_on_receivers_qt_thread(legacy_python):
    legacy_python(
        "driving",
        """
        import asyncio
        import threading
        import time
        from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSlot
        from rd_driving.infrastructure.workers.signal_bridge import SignalBridge
        from rd_driving.infrastructure.workers.control_system_thread import ControlSystemThread
        from rd_core.settings import DrivingSettings
        from rd_core.samples import ControlInput

        app = QCoreApplication([])
        received = []
        class Receiver(QObject):
            @pyqtSlot(dict)
            def update(self, report):
                received.append((report, QThread.currentThread()))
        receiver = Receiver()
        bridge = SignalBridge()
        bridge.control_data_updated.connect(receiver.update)
        worker = ControlSystemThread(bridge, DrivingSettings(session=42))
        sample = ControlInput(0.01, 0.02, 0.03, time.monotonic(), time.time_ns() // 1_000_000)
        async def emit():
            worker._execution(sample, None, 4.0)
        thread = threading.Thread(target=lambda: asyncio.run(emit()))
        thread.start()
        deadline = time.monotonic() + 2
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(received) == 1 and received[0][1] == app.thread()
        report = received[0][0]
        assert report['sample'] is sample and report['elapsed_ms'] == 4.0
        assert report['deadline'] == sample.sampled_at + 0.3
    """,
    )

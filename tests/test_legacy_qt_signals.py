def test_control_worker_delivers_state_on_receivers_qt_thread(legacy_python):
    legacy_python("driving", """
        import asyncio
        import threading
        import time
        from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSlot
        from infrastructure.workers.signal_bridge import SignalBridge
        from infrastructure.workers.control_system_thread import ControlSystemThread
        from infrastructure.protocol import ControlCodec

        app = QCoreApplication([])
        received = []
        class Receiver(QObject):
            @pyqtSlot(float, float, float)
            def update(self, v, w, rtt):
                received.append((v, w, rtt, QThread.currentThread()))
        receiver = Receiver()
        bridge = SignalBridge()
        bridge.control_data_updated.connect(receiver.update)
        worker = ControlSystemThread(bridge, '42')
        ControlCodec.process_state = staticmethod(lambda *args: (1.0, 2.0, 3.0))
        async def emit():
            worker._running = True
            await worker._state_queue.put({})
            task = asyncio.create_task(worker._run_state())
            await asyncio.sleep(0.02)
            worker._running = False
            task.cancel()
            await task
        thread = threading.Thread(target=lambda: asyncio.run(emit()))
        thread.start()
        deadline = time.monotonic() + 2
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert received == [(1.0, 2.0, 3.0, app.thread())], received
    """)

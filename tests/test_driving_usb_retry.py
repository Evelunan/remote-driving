import asyncio

import pytest
from rd_core.settings import ClientNetworkSettings, DrivingSettings
from rd_driving.app.input_service import DrivingInputService


class Device:
    def __init__(self):
        self.attempts = 0
        self.available = False
        self.connected = False
        self.disconnects = 0

    def set_data_callback(self, callback):
        self.callback = callback

    async def connect(self):
        self.attempts += 1
        if not self.available:
            raise ConnectionError("USB device not found")
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("connected_at_shutdown", [False, True])
async def test_usb_attempts_only_at_startup_and_on_request(connected_at_shutdown):
    device = Device()
    statuses = []
    service = DrivingInputService(
        DrivingSettings(network=ClientNetworkSettings(reconnect_delay_s=0.01)),
        device_factory=lambda *args: device,
        on_usb=lambda *args: statuses.append(args),
    )
    task = asyncio.create_task(service._usb())
    try:
        await until(lambda: statuses[-1:] == [(False, "未连接")])
        await asyncio.sleep(0.05)
        assert device.attempts == 1
        assert (await service.raw.get()).data is None

        # Repeated clicks result in exactly one additional attempt.
        assert service.request_usb_connect()
        assert not service.request_usb_connect()
        await until(lambda: device.attempts == 2)
        await asyncio.sleep(0.05)
        assert device.attempts == 2

        device.available = True
        assert service.request_usb_connect()
        await until(lambda: (True, "已连接") in statuses)
        assert device.attempts == 3
        assert not service.request_usb_connect()

        if not connected_at_shutdown:
            # Unplug invalidates input, but never starts another probe.
            device.connected = False
            await until(lambda: statuses[-1] == (False, "未连接"))
            assert (await service.raw.get()).data is None
            await asyncio.sleep(0.05)
            assert device.attempts == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert not device.connected
    assert device.disconnects == 3
    assert statuses[-1] == (False, "离线")
    assert not service.request_usb_connect()


def test_manual_usb_button_routes_to_worker_and_tracks_status(legacy_python):
    legacy_python("driving", """
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PyQt6.QtWidgets import QApplication
        from rd_driving.main_window import MainWindow
        app = QApplication([])
        window = MainWindow()
        window._setup_control_page()
        window._services_running = True
        class Worker:
            attempts = 0
            def request_usb_connect(self):
                self.attempts += 1
                return True
            def request_stop(self):
                pass
            def isRunning(self):
                return False
        worker = window._control_thread = Worker()
        button = window._control_panel._usb_connect_btn
        assert not button.isEnabled()
        window._on_usb_status_changed(False, '未连接')
        assert button.isEnabled()
        button.click()
        button.click()
        assert worker.attempts == 1
        assert not button.isEnabled()
        window._on_usb_status_changed(False, '连接中')
        assert not button.isEnabled()
        window._on_usb_status_changed(True, '已连接')
        assert not button.isEnabled()
        window._on_usb_status_changed(False, '未连接')
        button.click()
        assert worker.attempts == 2
        window.close()
        app.processEvents()
        app.processEvents()
        assert window._closing_ready
    """)


def test_usb_request_runs_on_worker_loop_and_stops_cleanly(legacy_python):
    legacy_python("driving", """
        import asyncio
        import threading
        from PyQt6.QtCore import QCoreApplication
        from rd_core.settings import DrivingSettings
        from rd_driving.infrastructure.workers import control_system_thread as module
        from rd_driving.infrastructure.workers.signal_bridge import SignalBridge
        app = QCoreApplication([])
        ready, called = threading.Event(), threading.Event()
        threads = []
        class Service:
            def __init__(self, *args, **kwargs):
                pass
            async def run(self):
                ready.set()
                await asyncio.Event().wait()
            def request_usb_connect(self):
                threads.append(threading.get_ident())
                called.set()
        module.DrivingInputService = Service
        bridge = SignalBridge()
        worker = module.ControlSystemThread(bridge, DrivingSettings())
        assert not worker.request_usb_connect()
        worker.start()
        try:
            assert ready.wait(2)
            assert worker.request_usb_connect()
            assert called.wait(2)
            assert threads[0] != threading.get_ident()
        finally:
            worker.request_stop()
            assert worker.wait(2000)
        assert not worker.request_usb_connect()
    """)

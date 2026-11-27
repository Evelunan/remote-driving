import asyncio
import threading
import time

import pytest
from rd_core.async_compat import async_timeout
from rd_core.samples import ControlInput
from rd_core.settings import VideoSafetySettings
from rd_core.video_safety import VideoGate, VideoSource
from rd_driving.app.control_client import DrivingControlClient


def test_unique_pictures_recovery_outage_revision_and_source_replacement():
    source = VideoSource("vehicle", 42, "publisher")
    gate = VideoGate(stale_frame_ms=100, recovery_ms=50)
    assert not gate.snapshot(now=10).ready
    assert gate.observe(1, 10, source, captured_at=10, now=10)
    assert not gate.snapshot(now=10.06).ready  # Time alone is not new picture evidence.
    assert gate.observe(2, 10.06, source, captured_at=10.06, now=10.06)
    ready = gate.snapshot(now=10.06)
    assert ready.ready and ready.ready_at == 10.06
    assert not gate.observe(2, 10.12, source, captured_at=10.12, now=10.12)
    assert not gate.snapshot(now=10.17).ready
    gate.observe(3, 10.18, source, captured_at=10.18, now=10.18)
    gate.observe(4, 10.24, source, captured_at=10.24, now=10.24)
    recovered = gate.snapshot(now=10.24)
    assert recovered.ready and recovered.revision != ready.revision
    gate.invalidate("hidden")
    assert not gate.observe(4, 10.25, source, captured_at=10.25, now=10.25)
    assert not gate.observe(5, float("nan"), source, now=10.25)
    assert not gate.observe(5, 11, source, captured_at=11, now=10.25)
    replacement = VideoSource("vehicle", 42, "replacement-publisher")
    gate.observe(1, 10.26, replacement, captured_at=10.26, now=10.26)
    assert not gate.snapshot(now=10.26).ready
    gate.observe(2, 10.32, replacement, captured_at=10.32, now=10.32)
    gate.invalidate("old disconnect", source=source)
    assert gate.snapshot(now=10.32).ready


def test_capture_deadline_expires_even_if_gui_stops_and_unknown_breaks_recovery():
    gate = VideoGate(stale_frame_ms=500, max_capture_age_ms=150, recovery_ms=0)
    source = VideoSource("vehicle", 42, "publisher")
    assert gate.observe(1, 10, source, captured_at=9.90, now=10)
    assert not gate.snapshot(now=10.06).ready
    assert not gate.observe(2, 10.06, source, captured_at=9.7, now=10.06)
    assert gate.observe(3, 10.07, source, captured_at=10.05, now=10.07)
    # Two unique paints may share one Windows monotonic clock tick.
    assert not gate.observe(4, 10.07, source, now=10.08)
    assert not gate.snapshot(now=10.08).ready


def test_only_completed_primary_qt_paints_refresh_video_health(
    legacy_python,
):
    legacy_python(
        "driving",
        r"""
        import os, time
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        import numpy as np
        from PyQt6.QtWidgets import QApplication
        from rd_core.video_safety import VideoGate, VideoSource
        from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
        from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
        now = 10.0
        time.monotonic = lambda: now
        app = QApplication([])
        gate = VideoGate(stale_frame_ms=100, recovery_ms=20)
        cache = FrameBuffer()
        source = VideoSource("vehicle", 42, "publisher")
        widget = MultiVideoWidget(cache, stale_frame_ms=100, video_gate=gate)
        widget.resize(640, 480)
        widget.show()
        app.processEvents()
        pixels = np.zeros((64,96,3),dtype=np.uint8)
        def publish(view="front", paint=True):
            cache.put(view,pixels,source=source,captured_at=time.monotonic())
            widget.update()
            if paint:
                app.processEvents()
        publish(paint=False)
        assert not gate.snapshot().ready
        app.processEvents()
        now += 0.03
        publish()
        assert gate.snapshot().ready
        now += 0.12  # Expiry is visible even if Qt stops processing events.
        assert not gate.snapshot().ready
        for _ in range(3):
            publish("back")
            now += 0.02
        assert not gate.snapshot().ready
        publish()
        now += 0.03
        publish()
        assert gate.snapshot().ready
        widget.hide()
        assert not gate.snapshot().ready
        for _ in range(3):
            publish()
            now += 0.02
        assert not gate.snapshot().ready
        widget.show()
        publish()
        now += 0.03
        publish()
        assert gate.snapshot().ready
        widget.close()
    """,
    )


def test_live_gui_keeps_status_during_manual_control_and_video_lifecycle(
    legacy_python, runtime_factory, tmp_path
):
    build, _ = runtime_factory
    build(
        ("relay", "signaling")
    )  # Write the fixture config; subprocess owns listeners.
    code = r"""
        import asyncio, os, threading, time
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from dataclasses import replace
        import aioice.ice
        aioice.ice.get_host_addresses = lambda **kwargs: ["127.0.0.1"]
        from PyQt6.QtWidgets import QApplication
        from rd_core.settings import CameraSettings, ClientNetworkSettings, CredentialSettings, DrivingSettings, VehicleSettings
        from rd_core.samples import RawInput
        from rd_core.wire import VehicleStatus
        from rd_server.main import create_runtime, parse_args
        from rd_vehicle.main import run
        from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
        from rd_vehicle.infrastructure.controller.mock_controller import MockController
        from rd_vehicle.infrastructure.controller.vehicle_feedback import FeedbackSample
        from rd_driving.main_window import MainWindow
        import rd_driving.app.input_service as inputs
        shared, errors = {}, []
        ready = threading.Event()
        camera_silent = threading.Event()
        delayed_capture = threading.Event()
        class Camera(TestCamera):
            def _read(self):
                if camera_silent.is_set():
                    return None
                return super()._read()

            def get_snapshot(self):
                image = super().get_snapshot()
                if image is not None and delayed_capture.is_set():
                    return replace(image, captured_at=image.captured_at - 0.35)
                return image

        class Feedback:
            speed = 1.25
            revision = 0
            def latest(self):
                self.revision += 1
                return FeedbackSample(VehicleStatus(speed_mps=self.speed),
                                      time.time_ns() // 1_000_000, time.monotonic(), self.revision)
        async def backend():
            server = create_runtime(parse_args(["--config", CONFIG_PATH]))
            try:
                await server.start()
                network = ClientNetworkSettings(
                    signaling_url=f"wss://127.0.0.1:{server.services['signaling']._server.sockets[0].getsockname()[1]}",
                    signaling_ca_file=server.services['signaling'].config.tls.cert,
                    quic_host="127.0.0.1",quic_server_name="localhost",
                    quic_port=server.services['relay'].listener._transport.get_extra_info('sockname')[1],
                    quic_ca_file=server.services['signaling'].config.tls.cert,reconnect_delay_s=0.05)
                settings = VehicleSettings(session=42,room="video",controller="mock",network=network,
                    credentials=CredentialSettings("TEST_VEHICLE"),cameras=(CameraSettings("front","synthetic",96,64,30),))
                controller = MockController(settings.motion)
                controller.feedback = Feedback()
                stop = asyncio.Event()
                shared.update(network=network, controller=controller, stop=stop, loop=asyncio.get_running_loop())
                ready.set()
                await run(settings, stop, controller_factory=lambda settings: controller,
                          camera_factory=lambda cam: Camera(cam.width, cam.height, cam.fps, cam.name))
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                ready.set()
                await server.stop()
        thread = threading.Thread(target=lambda: asyncio.run(backend()),daemon=True)
        class Device:
            axis = 255  # The very first fresh input is full brake.
            reporting = True
            task = None
            def __init__(self,*args): pass
            def set_data_callback(self,callback): self.callback=callback
            async def connect(self): self.task=asyncio.create_task(self.produce())
            def is_connected(self): return self.task is not None and not self.task.done()
            async def produce(self):
                while True:
                    if self.reporting:
                        await self.callback(RawInput.capture(bytes((0,0,0,128,self.axis))))
                    await asyncio.sleep(0.01)
            async def disconnect(self):
                if self.task is not None:
                    self.task.cancel()
                    await asyncio.gather(self.task,return_exceptions=True)
        inputs.USBConnector=Device
        app=QApplication([])
        thread.start()
        window=None
        def until(predicate,timeout=4):
            deadline=time.monotonic()+timeout
            while not predicate():
                assert not errors, errors
                if time.monotonic() >= deadline:
                    service = window._control_thread.service
                    raise AssertionError((window.video_gate.snapshot(), service._control_state, service._control_detail, service._usb_online))
                app.processEvents()
                time.sleep(0.005)
        try:
            assert ready.wait(3) and not errors, errors
            settings=DrivingSettings(session=42,room="video",network=shared['network'],credentials=CredentialSettings("TEST_DRIVER"))
            window=MainWindow(session_id=42,room="video",settings=settings)
            window.show()
            panel=window._control_panel
            media_button=panel._action_buttons['video']
            control_button=panel._action_buttons['control']
            speed=panel._vehicle_stats['speed_mps']
            until(lambda: control_button.isEnabled() and speed.get_value() == '1.25')
            client=window._control_thread.service.client
            connection=client.connection
            assert window._receiver_thread is None and not client.control_requested
            assert media_button.text() == '连接视频' and control_button.text() == '开始控制'
            acknowledgements = []
            window._bridge.control_data_updated.connect(lambda data: acknowledgements.append(data["sample"]))
            def check_new_brake_ack():
                requested = time.monotonic()
                Device.axis = 255
                until(lambda: acknowledgements and acknowledgements[-1].sampled_at >= requested
                      and acknowledgements[-1].brake == 1 and shared['controller'].commands[-1] == (0, 0))

            control_button.click()
            until(lambda: acknowledgements and client.armed)
            assert acknowledgements[0].brake == 1
            epoch = client.epoch
            assert epoch is not None and window._receiver_thread is None
            media_button.click()
            until(lambda: window.video_gate.snapshot().ready)
            assert window.video_gate.snapshot().source.principal == "vehicle"
            Device.axis = 64
            until(lambda: shared['controller'].commands[-1] == (2.5, 0.0))
            # Stop only camera frames. Both transports, the visible dashboard and
            # the USB reporting cadence remain live throughout the expiry.
            receiver = window._receiver_thread
            camera_silent.set()
            display = window._multi_video_widget.get_display('front')
            until(lambda: display._message == '接收画面已过期'
                  and not panel.get_connection_items()['video'].is_online())
            assert not window.video_gate.snapshot().ready
            assert window._webrtc_connected and receiver.isRunning() and display.isVisible()
            check_new_brake_ack()
            assert client.armed and client.epoch == epoch and client.connection is connection
            assert panel.get_connection_items()['control'].is_online()
            assert window._navbar._control_status.text() == '发送中'
            assert control_button.text() == '停止控制'
            # Deliver new frames again, with capture timestamps beyond the
            # driver's 300 ms limit but within the sender's 500 ms frame window.
            delayed_capture.set()
            camera_silent.clear()
            video_row = panel.get_connection_items()['video']
            until(lambda: video_row._status_label.text() == '主画面延迟未知或过高'
                  and window.fb.get('front').captured_at is not None)
            Device.axis = 64
            control_button.click()
            until(lambda: client.epoch != epoch and control_button.text() == '开始控制')
            states = []
            window._bridge.control_status_changed.connect(lambda state, text: states.append(state))
            last_frame = window.fb.get('front').id
            started = time.monotonic()
            control_button.click()
            until(lambda: 'active' in states
                  and acknowledgements and acknowledgements[-1].sampled_at >= started
                  and acknowledgements[-1].throttle > 0 and acknowledgements[-1].brake == 0
                  and window.fb.get('front').id > last_frame)
            assert client.armed and client.epoch is not None and client.epoch != epoch
            epoch = client.epoch
            assert client.connection is connection and window._receiver_thread is receiver
            frame = window.fb.get('front')
            assert frame.received_at - frame.captured_at >= settings.video.max_capture_age_ms / 1000
            assert window.video_gate.snapshot().reason == 'capture_unknown_or_expired'
            assert video_row._status_label.text() == '主画面延迟未知或过高'
            assert panel.get_connection_items()['control'].is_online()
            assert window._navbar._control_status.text() == '发送中'
            assert control_button.text() == '停止控制'
            Device.axis = 64
            until(lambda: shared['controller'].commands[-1] == (2.5, 0.0))
            check_new_brake_ack()
            assert client.armed and client.epoch == epoch and not window.video_gate.snapshot().ready
            delayed_capture.clear()
            Device.axis = 64
            until(lambda: shared['controller'].commands[-1] == (2.5, 0.0))
            until(lambda: window.video_gate.snapshot().ready)
            window._multi_video_widget.hide()
            until(lambda: not window.video_gate.snapshot().ready)
            media_button.click()
            until(lambda: window._receiver_thread is None)
            check_new_brake_ack()
            assert client.armed and client.epoch == epoch and client.connection is connection
            assert control_button.text() == '停止控制'
            shared['controller'].feedback.speed = 0.5
            until(lambda: speed.get_value() == '0.5')
            Device.axis = 64
            until(lambda: shared['controller'].commands[-1] == (2.5, 0.0))
            window._multi_video_widget.show()
            media_button.click()
            until(lambda: window.video_gate.snapshot().ready)
            assert client.epoch == epoch
            receiver = window._receiver_thread
            # Drop the driver's actual QUIC connection while the accelerator is held.
            window._control_thread._loop.call_soon_threadsafe(connection.protocol.close)
            until(lambda: not client.armed and shared['controller'].commands[-1] == (0, 0))
            last_frame = window.fb.get('front').id
            until(lambda: window.fb.get('front').id > last_frame and window.video_gate.snapshot().ready)
            assert receiver is window._receiver_thread and receiver.isRunning()
            until(lambda: client.epoch is not None and client.epoch != epoch)
            assert not client.armed and control_button.text() == '停止控制'
            Device.axis = 128
            until(lambda: client.armed and client.epoch != epoch)
            # A silent USB device pauses control. Cancelling the request while
            # waiting must keep control off when the device starts reporting again.
            Device.reporting = False
            until(lambda: client.state == 'waiting_input' and not client.armed)
            until(control_button.isEnabled)
            control_button.click()
            until(lambda: control_button.text() == '开始控制' and not client.control_requested)
            Device.reporting = True
            resumed_at = time.monotonic()
            until(lambda: client.latest.sampled_at > resumed_at + 0.4)
            assert not client.armed and not client.control_requested and receiver.isRunning()
        finally:
            if window is not None:
                window.close()
                until(lambda: window._closing_ready)
                app.processEvents()
            if 'loop' in shared:
                shared['loop'].call_soon_threadsafe(shared['stop'].set)
            thread.join(4)
        assert not thread.is_alive() and not errors, errors
        assert shared['controller'].closed
    """
    legacy_python(
        "driving",
        code.replace("CONFIG_PATH", repr(str(tmp_path / "authenticated-server.yaml"))),
    )


@pytest.mark.asyncio
async def test_manual_stop_before_vehicle_connects_cancels_future_commands(runtime_factory, local_tls):
    from rd_core.settings import ClientNetworkSettings, ControlSettings, CredentialSettings, DrivingSettings
    from rd_core.wire import CONTROL_REVISION, Packet, Window, parse_control_route
    from rd_quic.client import registered_connection

    build, _ = runtime_factory
    server = build()
    await server.start()
    network = ClientNetworkSettings(
        quic_host="127.0.0.1",
        quic_port=server.services["relay"].listener._transport.get_extra_info("sockname")[1],
        quic_server_name="localhost", quic_ca_file=local_tls[0],
    )
    client = DrivingControlClient(
        DrivingSettings(session=42, network=network, credentials=CredentialSettings("TEST_DRIVER")),
        asyncio.Queue(),
    )
    driver = asyncio.create_task(client.run())
    events, commands = asyncio.Queue(), []
    try:
        async with async_timeout(3):
            while client.connection is None:
                await asyncio.sleep(0.01)
        assert client.request_start_control()
        assert client.request_stop_control()
        async with registered_connection(
            network, CredentialSettings("TEST_VEHICLE"), ControlSettings(), session=42,
            role="vehicle", revision=CONTROL_REVISION, datagram_handler=commands.append,
            event_handler=lambda method, params: events.put_nowait((method, params)),
        ) as vehicle:
            method, params = await asyncio.wait_for(events.get(), 3)
            assert method == "control_route"
            _, epoch, _ = parse_control_route(params)
            assert epoch is not None
            for seq in range(15):
                vehicle.protocol.send_datagram(Packet(42, epoch, seq, 0, Window(seq.to_bytes(16, "big"))).encode())
                await client.samples.put(ControlInput(0.5, 0, 0, time.monotonic(), time.time_ns() // 1_000_000))
                await asyncio.sleep(0.02)
            assert client.epoch == epoch and not client.control_requested
            assert not commands
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        await server.stop()


@pytest.mark.asyncio
async def test_frozen_primary_is_invalidated_before_its_blocked_capture_finishes():
    from rd_vehicle.application.multi_camera_manager import MultiCameraManager
    from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
    from test_video_sources import LocalPublisher

    entered, release = threading.Event(), threading.Event()

    class Frozen(TestCamera):
        count = 0

        def _read(self):
            self.count += 1
            if self.count > 1:
                entered.set()
                release.wait(2)
            return super()._read()

    publisher = LocalPublisher()
    invalidated = asyncio.Event()
    original = publisher.invalidate_frame

    def invalidate(view):
        original(view)
        invalidated.set()

    publisher.invalidate_frame = invalidate
    manager = MultiCameraManager(
        publisher,
        video_policy=VideoSafetySettings(stale_frame_ms=100),
    )
    camera = Frozen(96, 64, 30, "front")
    manager.add_camera(camera)
    waiting = cleanup = None
    try:
        await manager.start()
        waiting = asyncio.create_task(manager.wait())
        await asyncio.wait_for(invalidated.wait(), 1)
        assert entered.is_set() and not release.is_set()
        assert not waiting.done()
        cleanup = asyncio.create_task(manager.stop())
        await asyncio.sleep(0.05)
        assert not cleanup.done()  # Fault notification didn't wait for read/close.
    finally:
        release.set()
        await manager.stop()
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)
        if cleanup is not None:
            await cleanup
        publisher.tracks.close()
    assert camera._thread is None

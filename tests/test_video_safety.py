import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rd_core.samples import ControlInput
from rd_core.settings import VideoSafetySettings
from rd_core.video_safety import VideoGate, VideoSource
from rd_driving.app.control_client import DrivingControlClient
from rd_quic.framing import RpcError


def test_unique_pictures_recovery_outage_revision_and_source_replacement():
    source = VideoSource("vehicle", 42, "publisher")
    gate = VideoGate(stale_frame_ms=100, recovery_ms=50)
    assert not gate.snapshot(now=10).ready
    assert gate.observe(1, 10, source, now=10)
    assert not gate.snapshot(now=10.06).ready  # Time alone is not new picture evidence.
    assert gate.observe(2, 10.06, source, now=10.06)
    ready = gate.snapshot(now=10.06)
    assert ready.ready and ready.ready_at == 10.06
    assert not gate.observe(2, 10.12, source, now=10.12)
    assert not gate.snapshot(now=10.17).ready
    gate.observe(3, 10.18, source, now=10.18)
    gate.observe(4, 10.24, source, now=10.24)
    recovered = gate.snapshot(now=10.24)
    assert recovered.ready and recovered.revision != ready.revision
    gate.invalidate("hidden")
    assert not gate.observe(4, 10.25, source, now=10.25)
    assert not gate.observe(5, float("nan"), source, now=10.25)
    assert not gate.observe(5, 11, source, now=10.25)
    replacement = VideoSource("vehicle", 42, "replacement-publisher")
    gate.observe(1, 10.26, replacement, now=10.26)
    assert not gate.snapshot(now=10.26).ready
    gate.observe(2, 10.32, replacement, now=10.32)
    gate.invalidate("old disconnect", source=source)
    assert gate.snapshot(now=10.32).ready


def test_only_completed_primary_qt_paints_allow_control_and_hidden_ui_expires(
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
            cache.put(view,pixels,source=source)
            widget.update()
            if paint:
                app.processEvents()
        publish(paint=False)
        assert not gate.snapshot().ready
        app.processEvents()
        time.sleep(0.03)
        publish()
        assert gate.snapshot().ready
        time.sleep(0.12)  # No Qt event processing: control still observes the expiry.
        assert not gate.snapshot().ready
        for _ in range(3):
            publish("back")
            time.sleep(0.02)
        assert not gate.snapshot().ready
        publish()
        time.sleep(0.03)
        publish()
        assert gate.snapshot().ready
        widget.hide()
        assert not gate.snapshot().ready
        for _ in range(3):
            publish()
            time.sleep(0.02)
        assert not gate.snapshot().ready
        widget.show()
        publish()
        time.sleep(0.03)
        publish()
        assert gate.snapshot().ready
        widget.close()
    """,
    )


def test_live_gui_media_and_control_revoke_on_hidden_picture_then_require_neutral(
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
        from rd_server.main import create_runtime, parse_args
        from rd_vehicle.main import run
        from rd_vehicle.infrastructure.controller.mock_controller import MockController
        from rd_driving.main_window import MainWindow
        import rd_driving.app.input_service as inputs
        shared, errors = {}, []
        ready = threading.Event()
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
                stop = asyncio.Event()
                shared.update(network=network, controller=controller, stop=stop, loop=asyncio.get_running_loop())
                ready.set()
                await run(settings,stop,controller_factory=lambda settings: controller)
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                ready.set()
                await server.stop()
        thread = threading.Thread(target=lambda: asyncio.run(backend()),daemon=True)
        class Device:
            axis = 128
            task = None
            def __init__(self,*args): pass
            def set_data_callback(self,callback): self.callback=callback
            async def connect(self): self.task=asyncio.create_task(self.produce())
            def is_connected(self): return self.task is not None and not self.task.done()
            async def produce(self):
                while True:
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
                assert time.monotonic()<deadline, "GUI condition timed out"
                app.processEvents()
                time.sleep(0.005)
        try:
            assert ready.wait(3) and not errors, errors
            settings=DrivingSettings(session=42,room="video",network=shared['network'],credentials=CredentialSettings("TEST_DRIVER"))
            window=MainWindow(session_id=42,room="video",settings=settings)
            window.show()
            until(lambda: window._control_thread.service is not None and window._control_thread.service.client.armed)
            client=window._control_thread.service.client
            epoch=client.epoch
            assert window.video_gate.snapshot().source.principal == "vehicle"
            Device.axis=64
            until(lambda: shared['controller'].commands[-1] == (2.5,0.0))
            window._multi_video_widget.hide()
            until(lambda: not client.armed and shared['controller'].commands[-1] == (0,0))
            window._multi_video_widget.show()
            until(lambda: window.video_gate.snapshot().ready)
            assert not client.armed and shared['controller'].commands[-1] == (0,0)
            Device.axis=128
            until(lambda: client.armed)
            assert client.epoch != epoch
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
async def test_video_loss_during_acquire_cannot_reuse_recovered_picture_revision():
    from rd_core.settings import DrivingSettings

    gate = VideoGate(recovery_ms=0)
    source = VideoSource("vehicle", 42, "publisher")
    gate.observe(1, time.monotonic(), source)
    entered, resume = asyncio.Event(), asyncio.Event()
    sent = []

    async def request(method, params):
        entered.set()
        await resume.wait()
        return {
            "epoch": str(uuid4()),
            "session": 42,
            "neutral_required": True,
            "vehicle_principal": "vehicle",
        }

    client = DrivingControlClient(
        DrivingSettings(session=42), asyncio.Queue(), video_gate=gate
    )
    client.latest = ControlInput(0, 0, 0, time.monotonic(), time.time_ns() // 1_000_000)
    client.connection = SimpleNamespace(
        protocol=SimpleNamespace(
            rpc_ready=True, request=request, send_datagram=sent.append
        ),
        clock=SimpleNamespace(check=lambda: None),
    )
    task = asyncio.create_task(client._send_session())
    await entered.wait()
    gate.invalidate()
    gate.observe(2, time.monotonic(), source)
    resume.set()
    with pytest.raises(RpcError, match="primary_video_unavailable"):
        await task
    assert not sent and not client.armed


@pytest.mark.asyncio
async def test_real_control_requires_matching_live_video_and_neutral_after_recovery(
    runtime_factory, local_tls
):
    from rd_core.settings import (
        ClientNetworkSettings,
        CredentialSettings,
        DrivingSettings,
        VehicleSettings,
    )
    from rd_vehicle.application.control_client import VehicleControlClient
    from rd_vehicle.infrastructure.controller.mock_controller import MockController

    build, _ = runtime_factory
    server = build()
    await server.start()
    network = ClientNetworkSettings(
        quic_host="127.0.0.1",
        quic_ca_file=local_tls[0],
        quic_server_name="localhost",
        quic_port=server.services["relay"].listener._transport.get_extra_info(
            "sockname"
        )[1],
        reconnect_delay_s=0.03,
    )
    vehicle_settings = VehicleSettings(
        session=42,
        controller="mock",
        network=network,
        credentials=CredentialSettings("TEST_VEHICLE"),
    )
    controller = MockController(vehicle_settings.motion)
    vehicle = VehicleControlClient(vehicle_settings, controller)
    gate = VideoGate(stale_frame_ms=100, recovery_ms=50)
    queue = asyncio.Queue(maxsize=1)
    statuses = []
    driver = DrivingControlClient(
        DrivingSettings(
            session=42, network=network, credentials=CredentialSettings("TEST_DRIVER")
        ),
        queue,
        video_gate=gate,
        on_status=lambda *args: statuses.append(args),
    )
    live, throttle, source = False, 0, VideoSource("vehicle", 42, "publisher")

    async def produce():
        sequence = 0
        while True:
            sequence += 1
            if live:
                gate.observe(sequence, time.monotonic(), source)
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(
                ControlInput(
                    throttle, 0, 0, time.monotonic(), time.time_ns() // 1_000_000
                )
            )
            await asyncio.sleep(0.01)

    tasks = [
        asyncio.create_task(job) for job in (vehicle.run(), driver.run(), produce())
    ]

    async def until(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    try:
        await until(lambda: driver.state == "waiting_video")
        assert vehicle.epoch is None
        live = True
        await until(lambda: driver.armed)
        epoch = driver.epoch
        throttle = 0.5
        await until(lambda: controller.commands[-1][0] == 2.5)
        live = False
        await until(
            lambda: (
                not driver.armed
                and vehicle.epoch is None
                and controller.commands[-1] == (0, 0)
            )
        )
        assert ("video_unavailable", "primary_video_unavailable") in statuses
        live = True
        await asyncio.sleep(0.2)
        assert not driver.armed  # Picture recovery doesn't restore held throttle.
        throttle = 0
        await until(lambda: driver.armed)
        assert driver.epoch != epoch
        source = VideoSource("vehicle", 43, "wrong-session")
        await until(lambda: not driver.armed and vehicle.epoch is None)
        await asyncio.sleep(0.15)
        assert not driver.armed
        source = VideoSource("another-vehicle", 42, "wrong-identity")
        await until(
            lambda: ("video_vehicle_mismatch", "video_vehicle_mismatch") in statuses
        )
        assert not driver.armed and not any(
            v or w for v, w in list(controller.commands)[-3:]
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.stop()


@pytest.mark.asyncio
async def test_primary_capture_freeze_reports_fault_before_blocked_driver_cleanup():
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
    fault = asyncio.Event()
    manager = MultiCameraManager(
        publisher,
        video_policy=VideoSafetySettings(stale_frame_ms=100),
        on_fault=fault.set,
    )
    camera = Frozen(96, 64, 30, "front")
    manager.add_camera(camera)
    waiting = cleanup = None
    try:
        await manager.start()
        waiting = asyncio.create_task(manager.wait())
        await asyncio.wait_for(fault.wait(), 1)
        assert entered.is_set() and not release.is_set()
        with pytest.raises(RuntimeError, match="primary capture expired"):
            await waiting
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

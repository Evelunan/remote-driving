import asyncio
import json
import os
import subprocess
import sys
import time

import pytest
import yaml
from rd_core.async_compat import async_timeout
from rd_core.samples import RawInput
from rd_core.settings import (
    CameraEncodingSettings,
    CameraSettings,
    ClientNetworkSettings,
    CredentialSettings,
    DrivingSettings,
    MotionSettings,
    USBSettings,
    VehicleSettings,
)
from rd_core.video_safety import VideoGate
from rd_driving.app.input_service import DrivingInputService
from rd_driving.cli import parse_args as driving_args
from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.controller.mock_controller import MockController
from rd_vehicle.main import parse_args as vehicle_args
from rd_vehicle.main import run as run_vehicle


@pytest.mark.parametrize("role", ["driving", "vehicle"])
def test_client_check_dump_are_headless_and_do_not_resolve_dump_secrets(
    role, tmp_path, local_tls
):
    config = {
        "credentials": {"token_env": "CHECK_CLIENT_TOKEN"},
        "network": {
            "signaling_ca_file": str(local_tls[0]),
            "quic_ca_file": str(local_tls[0]),
        },
    }
    if role == "vehicle":
        config["controller"] = "mock"
    path = tmp_path / "client.yaml"
    path.write_text(yaml.safe_dump(config))
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["CHECK_CLIENT_TOKEN"] = "test-" + "x" * 40
    module, entry = (
        ("rd_driving.cli", "main") if role == "driving" else ("rd_vehicle.main", "cli")
    )
    for mode in ("--check-config", "--dump-config"):
        if mode == "--dump-config":
            env.pop("CHECK_CLIENT_TOKEN")
        code = f"""import sys
from {module} import {entry}
assert {entry}({["--config", str(path), mode]!r}) == 0
assert not {{"cv2", "rclpy", "PyQt6"}} & set(sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "test-" + "x" * 40 not in result.stdout + result.stderr
        if mode == "--dump-config":
            dumped = json.loads(result.stdout)
            assert dumped["config"]["credentials"]["token_env"] == "CHECK_CLIENT_TOKEN"


def test_client_cli_precedence_separate_instances_and_no_implicit_room_override(
    tmp_path, monkeypatch
):
    path = tmp_path / "driving.yaml"
    path.write_text(
        "room: named-room\nusb:\n  vendor_id: 100\nui:\n  frame_interval_ms: 20\n"
    )
    monkeypatch.setenv("RD_DRIVING__USB__VENDOR_ID", "200")
    a = driving_args(
        ["--config", str(path), "--session", "0", "--set", "usb.vendor_id=300"]
    )
    b = driving_args(["--config", str(path)])
    assert a.loaded.value.session == 0 and a.loaded.value.room == "named-room"
    assert a.loaded.value.usb.vendor_id == 300 and b.loaded.value.usb.vendor_id == 200
    assert a.loaded.value.ui.frame_interval_ms == 20
    with pytest.raises(SystemExit):
        driving_args(["--session", "1", "--set", "session=2"])
    with pytest.raises(SystemExit):
        vehicle_args(["--components", "control"])
    with pytest.raises(SystemExit):
        vehicle_args(["--set", "cameras=[{name: unknown}]"])
    with pytest.raises(SystemExit):
        vehicle_args(["--set", "cameras=[{fps: 241}]"])
    assert vehicle_args(
        ["--components", "control", "--set", "controller=mock"]
    ).loaded.value.components == ("control",)


@pytest.mark.asyncio
@pytest.mark.parametrize("budgeted", [False, True])
async def test_formal_vehicle_runtime_and_driving_input_execute_configured_values_with_video(
    runtime_factory, local_tls, monkeypatch, budgeted
):
    import aioice.ice
    from rd_webrtc import encoding

    encoders = []
    original = encoding.create_encoder

    def recorded(codec, policy):
        encoder = original(codec, policy)
        encoders.append(encoder)
        return encoder

    monkeypatch.setattr(encoding, "create_encoder", recorded)

    monkeypatch.setattr(
        aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
    )
    build, _ = runtime_factory
    server = build(("relay", "signaling"))
    await server.start()
    signaling_port = server.services["signaling"]._server.sockets[0].getsockname()[1]
    quic_port = server.services["relay"].listener._transport.get_extra_info("sockname")[
        1
    ]
    network = ClientNetworkSettings(
        signaling_url=f"wss://127.0.0.1:{signaling_port}",
        signaling_ca_file=local_tls[0],
        quic_host="127.0.0.1",
        quic_port=quic_port,
        quic_server_name="localhost",
        quic_ca_file=local_tls[0],
        reconnect_delay_s=0.05,
    )
    vehicle = VehicleSettings(
        session=42,
        room="video",
        controller="mock",
        encoder_workers=1 if budgeted else None,
        network=network,
        credentials=CredentialSettings("TEST_VEHICLE"),
        motion=MotionSettings(1.2, 0.4),
        cameras=(
            CameraSettings(
                "front",
                "synthetic",
                96,
                64,
                20,
                CameraEncodingSettings(800_000, 250_000, 1_000_000, 1)
                if budgeted
                else None,
            ),
            CameraSettings(
                "back",
                "synthetic",
                64,
                48,
                40,
                CameraEncodingSettings(300_000, 100_000, 500_000, 1)
                if budgeted
                else None,
            ),
        ),
    )
    driving = DrivingSettings(
        session=42,
        room="video",
        network=network,
        credentials=CredentialSettings("TEST_DRIVER"),
        usb=USBSettings(123, 456),
    )
    created = []
    controller = MockController(vehicle.motion)

    def camera_factory(settings):
        camera = TestCamera(
            settings.width, settings.height, settings.fps, settings.name
        )
        created.append(camera)
        return camera

    class Device:
        def __init__(self):
            self.axis = 128
            self.enabled = True
            self.task = None

        def set_data_callback(self, callback):
            self.callback = callback

        async def connect(self):
            self.task = asyncio.create_task(self.produce())

        def is_connected(self):
            return self.task is not None and not self.task.done()

        async def produce(self):
            while True:
                if self.enabled:
                    await self.callback(
                        RawInput.capture(
                            bytes((0, 0, 0, 64 if self.axis != 128 else 128, self.axis))
                        )
                    )
                await asyncio.sleep(0.01)

        async def disconnect(self):
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    device = Device()

    def device_factory(vendor, product):
        assert (vendor, product) == (123, 456)
        return device

    gate = VideoGate()
    inputs = DrivingInputService(
        driving, device_factory=device_factory, video_gate=gate
    )
    receiver = WebRTCReceiver("video", network, driving.credentials)
    frames = {}
    sequence = 0

    def frame_received(name, image, source, captured_at):
        nonlocal sequence
        frames[name] = image.shape
        sequence += 1
        if name == "front":
            gate.observe(sequence, time.monotonic(), source, captured_at=captured_at)

    receiver.on_frame(frame_received, include_capture_time=True)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(
            run_vehicle(
                vehicle,
                stop,
                controller_factory=lambda settings: controller,
                camera_factory=camera_factory,
            )
        ),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(receiver.start()),
    ]

    async def until(predicate):
        async with async_timeout(4):
            while not predicate():
                for task in tasks:
                    if task.done():
                        task.result()
                await asyncio.sleep(0.01)

    try:
        await until(lambda: inputs.client.armed and len(frames) == 2)
        assert frames == {"front": (64, 96, 3), "back": (48, 64, 3)}
        assert [camera.fps for camera in created] == [20, 40]
        if budgeted:
            assert {encoder.policy.target_bitrate_bps for encoder in encoders} == {
                800_000,
                300_000,
            }
            assert all(encoder.codec.thread_count == 1 for encoder in encoders)
            assert all(
                encoder.policy.min_bitrate_bps
                <= encoder.codec.bit_rate
                <= encoder.policy.max_bitrate_bps
                for encoder in encoders
            )
        device.axis = 64
        await until(lambda: (0.6, -0.2) in controller.commands)
        device.enabled = False
        await until(
            lambda: not inputs.client.armed and controller.commands[-1] == (0, 0)
        )
        stop.set()
        await asyncio.wait_for(tasks[0], 3)
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await receiver.close()
        await server.stop()
    assert controller.closed
    assert all(camera._thread is None for camera in created)
    assert device.task is not None and device.task.done()
    import threading

    assert not any(
        thread.name.startswith("rd-encode") for thread in threading.enumerate()
    )


def test_gui_settings_and_login_close_without_uninitialized_thread(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtWidgets import QApplication
        from rd_core.settings import DrivingSettings, UISettings, VideoSafetySettings
        from rd_driving.main_window import MainWindow
        app = QApplication([])
        settings = DrivingSettings(session=0, room="test-room", ui=UISettings(theme="dark_tech", width=1300, height=800, frame_interval_ms=23, stats_interval_ms=777, debug=True), video=VideoSafetySettings(stale_frame_ms=222))
        window = MainWindow(settings=settings)
        assert window.minimumWidth() == 1300 and window.minimumHeight() == 800
        assert window._login_page._session_input.text() == "0"
        assert window._login_page._room_input.text() == "test-room"
        assert window._login_page._debug_mode
        window._session_id, window._room = 0, "test-room"
        window._setup_control_page()
        window._setup_timers()
        assert window._frame_timer.interval() == 23
        assert window._stats_timer.interval() == 777
        assert window._multi_video_widget._stale_after_s == 0.222
        window._services_running = True
        window._on_vehicle_status({"linear_mps": 1.2, "angular_radps": 0.4})
        assert "1.20 m/s" in window._control_panel._measurement_note.text()
        window._telemetry_at -= 1
        window._expire_telemetry()
        assert "过期" in window._control_panel._measurement_note.text()
        window._on_vehicle_status({"linear_mps": 1.2, "angular_radps": 0.4})
        window._on_quic_status_changed(False, "离线")
        assert "过期" in window._control_panel._measurement_note.text()
        window._login_page.close()
        window.close()
        app.processEvents()
        app.processEvents()
        assert window._closing_ready
    """,
    )


def test_session_selection_keeps_field_feedback_and_keyboard_flow(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QFrame
        from rd_core.settings import DrivingSettings
        from rd_driving.factories import UIFactory
        from rd_driving.ui.widgets import LoginPage

        app = QApplication([])
        for name in ("modern_glass", "dark_tech"):
            page = LoginPage(
                UIFactory.set_theme(name),
                settings=DrivingSettings(session=0, room="configured-room"),
            )
            page.show()
            card = page.findChild(QFrame, "sessionForm")
            for width, height in ((1200, 750), (1920, 1080)):
                page.resize(width, height)
                app.processEvents()
                assert page.rect().contains(card.geometry())
                assert card.width() == 480
                assert abs(card.geometry().center().x() - page.rect().center().x()) <= 1
                assert page._login_btn.isVisible()

            room, session = page._room_input, page._session_input
            submissions = []
            page.login_success.connect(submissions.append)
            assert (room.text(), session.text()) == ("configured-room", "0")
            assert page._fake_login_btn is None
            room.setFocus()
            QTest.keyClick(room, Qt.Key.Key_Tab)
            assert session.hasFocus()
            QTest.keyClick(session, Qt.Key.Key_Tab)
            assert page._login_btn.hasFocus()
            room.setFocus()
            QTest.keyClick(room, Qt.Key.Key_Return)
            assert session.hasFocus()

            room.setText("r" * 129)
            QTest.keyClick(session, Qt.Key.Key_Return)
            assert room.hasFocus()
            assert "128" in room.accessibleDescription()
            room.selectAll()
            QTest.keyClicks(room, "selected-room")
            assert room.accessibleDescription() == "用于接收摄像头画面"

            session.setText("65536")
            QTest.keyClick(room, Qt.Key.Key_Return)
            QTest.keyClick(session, Qt.Key.Key_Return)
            assert session.hasFocus()
            assert "0–65535" in session.accessibleDescription()
            assert not submissions

            session.selectAll()
            QTest.keyClicks(session, "65535")
            QTest.keyClick(session, Qt.Key.Key_Return)
            assert submissions == [{"session_id": 65535, "room": "selected-room"}]
            assert "正在连接" in page._status_label.text()
            page.reset_status()
            assert not page._status_label.text()
            assert (room.text(), session.text()) == ("selected-room", "65535")
            page.close()
        """,
    )


def test_session_selection_debug_action_uses_configured_values(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        from rd_core.settings import DrivingSettings, UISettings
        from rd_driving.factories import UIFactory
        from rd_driving.ui.widgets import LoginPage

        app = QApplication([])
        page = LoginPage(
            UIFactory.set_theme("dark_tech"),
            settings=DrivingSettings(session=0, room="debug-room", ui=UISettings(debug=True)),
        )
        page.show()
        page._room_input.setText("edited-room")
        page._session_input.setText("12")
        submissions = []
        page.login_success.connect(submissions.append)
        page._fake_login_btn.click()
        assert not page._fake_login_btn.isEnabled()
        assert not page._login_btn.isEnabled()
        page._fake_login_btn.click()
        QTest.qWait(400)
        assert submissions == [{"session_id": 0, "room": "debug-room"}]
        assert page._login_btn.isEnabled()
        page.reset_status()
        assert (page._room_input.text(), page._session_input.text()) == ("edited-room", "12")
        assert not page._status_label.text()
        """,
    )

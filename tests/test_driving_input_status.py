import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rd_core.samples import ControlInput
from rd_core.settings import DrivingSettings
from rd_core.wire import Execution, ExecutionStatus, Packet, Window
from rd_driving.app.input_service import DrivingInputService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample_kind,usb_online,expected",
    [("missing", False, "waiting_device"), ("missing", True, "waiting_input"),
     ("invalid", True, "waiting_input"), ("expired", True, "waiting_input"),
     ("before_request", True, "waiting_input")],
)
async def test_wait_reason_matches_device_and_sample(sample_kind, usb_online, expected):
    statuses = []
    service = DrivingInputService(DrivingSettings(session=42),
                                  on_status=lambda state, detail: statuses.append(state))
    service._usb_status(usb_online, "已连接" if usb_online else "未连接")
    client = service.client
    client.connection = SimpleNamespace(protocol=SimpleNamespace(rpc_ready=True), max_control_age_ms=300)
    client._event(client._generation, "control_route", {
        "revision": 1, "epoch": str(uuid4()), "vehicle_principal": "vehicle",
    })
    now = time.monotonic()
    assert service.request_start_control()
    assert statuses[-1] == "starting"
    if sample_kind != "missing":
        await asyncio.sleep(0.02)
        client.latest = ControlInput(0.5, 0, 0,
            now - (1 if sample_kind == "expired" else 0.05)
            if sample_kind in {"expired", "before_request"} else time.monotonic(),
            time.time_ns() // 1_000_000, valid=sample_kind != "invalid")
    client._send_latest(time.monotonic())
    assert statuses[-1] == expected and not client.armed
    if expected == "waiting_device":
        service._usb_status(True, "已连接")
        assert statuses[-1] == "waiting_input"
        service._usb_status(False, "未连接")
        assert statuses[-1] == "waiting_device"


@pytest.mark.asyncio
@pytest.mark.parametrize("axes", [(0, 1, 0), (0.5, 0, 0), (0, 0, -0.5)])
async def test_first_fresh_input_sends_immediately_and_only_real_ack_confirms(axes):
    sent, confirmations, events = [], [], []
    service = DrivingInputService(DrivingSettings(session=42),
                                  on_execution=lambda *args: confirmations.append(args))
    client = service.client
    epoch = uuid4()
    protocol = SimpleNamespace(rpc_ready=True, send_datagram=sent.append,
                               discard_pending_datagrams=lambda: None,
                               notify=lambda *event: events.append(event))
    # A route event can precede the registration response. Collect windows while idle.
    client._event(client._generation, "control_route", {
        "revision": 1, "epoch": str(epoch), "vehicle_principal": "vehicle",
    })
    client.connection = SimpleNamespace(protocol=protocol, max_control_age_ms=300)
    client._datagram(client._generation, Packet(42, epoch, 0, 0, Window(b"w" * 16)).encode())
    client.latest = ControlInput(0.8, 0, 0, time.monotonic(), time.time_ns() // 1_000_000)
    assert service.request_start_control()
    client._send_latest(time.monotonic())
    assert sent == []
    await asyncio.sleep(0.02)
    client.latest = ControlInput(*axes, time.monotonic(), time.time_ns() // 1_000_000)
    client._send_latest(time.monotonic())
    first = Packet.decode(sent[0])
    assert (first.payload.throttle, first.payload.brake, first.payload.steering) == axes
    assert client.state == "active" and not client.armed and not confirmations
    await asyncio.sleep(0.02)
    client.latest = ControlInput(-0.25, 0, 0.75, time.monotonic(), time.time_ns() // 1_000_000)
    client._send_latest(time.monotonic())
    assert Packet.decode(sent[1]).payload.throttle == -0.25 and not confirmations
    ack = Packet(42, epoch, first.seq, first.source_time_ms, Execution(ExecutionStatus.EXECUTED, 0))
    client._datagram(client._generation, ack.encode())
    assert client.armed and len(confirmations) == 1 and not events
    assert service.request_stop_control()
    assert events == [("stop_control", {"epoch": str(epoch)})] and not client.armed
    client._datagram(client._generation, ack.encode())
    assert len(confirmations) == 1 and client.state == "idle"


@pytest.mark.asyncio
async def test_stop_before_registration_reply_keeps_pair_without_replaying_input():
    sent = []
    client = DrivingInputService(DrivingSettings(session=42)).client
    epoch = uuid4()
    client._event(client._generation, "control_route", {
        "revision": 1, "epoch": str(epoch), "vehicle_principal": "vehicle",
    })
    assert client.request_start_control() and client.request_stop_control()
    client.connection = SimpleNamespace(protocol=SimpleNamespace(rpc_ready=True, send_datagram=sent.append),
                                        max_control_age_ms=300)
    client._datagram(client._generation, Packet(42, epoch, 0, 0, Window(b"w" * 16)).encode())
    client.latest = ControlInput(0.4, 0, 0, time.monotonic(), 1)
    assert client.request_start_control()
    client._send_latest(time.monotonic())
    assert not sent
    await asyncio.sleep(0.02)
    client.latest = ControlInput(0, 1, 0, time.monotonic(), 2)
    client._send_latest(time.monotonic())
    assert client.epoch == epoch and Packet.decode(sent[0]).payload.brake == 1


@pytest.mark.asyncio
async def test_recovery_forwards_brake_without_replaying_held_motion_axes():
    sent = []
    client = DrivingInputService(DrivingSettings(session=42)).client
    client.connection = SimpleNamespace(protocol=SimpleNamespace(rpc_ready=True, send_datagram=sent.append),
                                        max_control_age_ms=300)
    assert client.request_start_control()
    for revision in (1, 2):
        epoch = uuid4()
        client._event(client._generation, "control_route", {
            "revision": revision, "epoch": str(epoch), "vehicle_principal": "vehicle",
        })
        client._datagram(client._generation, Packet(42, epoch, 0, 0, Window(b"w" * 16)).encode())
        await asyncio.sleep(0.02)
        client.latest = ControlInput(0.5, 0, 0.25, time.monotonic(), revision)
        client._send_latest(time.monotonic())
    assert len(sent) == 1 and client.state == "waiting_neutral"
    client.latest = ControlInput(0.5, 1, 0.25, time.monotonic(), 3)
    client._send_latest(time.monotonic())
    brake = Packet.decode(sent[-1]).payload
    assert (brake.throttle, brake.brake, brake.steering) == (0, 1, 0)
    client.latest = ControlInput(0.5, 0, 0.25, time.monotonic(), 4)
    client._send_latest(time.monotonic())
    assert len(sent) == 2 and client.state == "waiting_neutral"


def test_input_status_text_and_online_indicator(legacy_python):
    legacy_python(
        "driving",
        """
        from PyQt6.QtCore import QCoreApplication
        from rd_core.settings import DrivingSettings
        from rd_driving.infrastructure.workers.control_system_thread import ControlSystemThread
        from rd_driving.infrastructure.workers.signal_bridge import SignalBridge
        app = QCoreApplication([])
        bridge = SignalBridge()
        worker = ControlSystemThread(bridge, DrivingSettings())
        received = []
        control = []
        bridge.quic_status_changed.connect(lambda *args: received.append(args))
        bridge.control_status_changed.connect(lambda *args: control.append(args))
        worker._connection_status(True, 'registered')
        for state, text in [
            ('waiting_device', '等待控制设备'),
            ('waiting_input', '等待设备输入'),
            ('waiting_neutral', '请将控制输入归零'),
            ('waiting_control_window', '等待车端控制窗口'),
            ('waiting_channel', '等待数据通道'),
            ('active', '发送中'),
        ]:
            worker._status(state)
            assert control[-1] == (state, text)
            assert received[-1] == (True, '已认证')
        worker._connection_status(False, 'protocol_upgrade_required')
        assert received[-1] == (False, '控制协议不兼容，请同步升级')
    """,
    )


def test_click_while_control_worker_starts_is_applied_offline(legacy_python):
    legacy_python("driving", """
        import asyncio
        from unittest.mock import AsyncMock, patch
        from PyQt6.QtCore import QCoreApplication
        from rd_core.settings import DrivingSettings
        from rd_driving.app.input_service import DrivingInputService
        from rd_driving.infrastructure.workers.control_system_thread import ControlSystemThread
        from rd_driving.infrastructure.workers.signal_bridge import SignalBridge
        app = QCoreApplication([])
        worker = ControlSystemThread(SignalBridge(), DrivingSettings())
        assert worker.request_start_control()
        with patch.object(DrivingInputService, "run", new_callable=AsyncMock):
            asyncio.run(worker._main())
        assert worker.service.client.control_requested
        assert worker.service.client.state == "waiting_channel"
    """)

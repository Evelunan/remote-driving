import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rd_core.samples import ControlInput
from rd_core.settings import DrivingSettings
from rd_driving.app.input_service import DrivingInputService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample_kind,usb_online,video_ready,expected",
    [
        ("missing", False, True, "waiting_device"),
        ("missing", True, True, "waiting_input"),
        ("invalid", True, True, "waiting_input"),
        ("expired", True, True, "waiting_input"),
        ("before_video", True, True, "waiting_input"),
        ("steering", True, True, "waiting_neutral"),
        ("throttle", True, True, "waiting_neutral"),
        ("missing", False, False, "waiting_video"),
    ],
)
async def test_wait_reason_matches_device_sample_and_video(
    sample_kind, usb_online, video_ready, expected
):
    statuses = asyncio.Queue()
    service = DrivingInputService(
        DrivingSettings(session=42),
        on_status=lambda state, detail: statuses.put_nowait(state),
    )
    service._usb_status(usb_online, "已连接" if usb_online else "未连接")
    client = service.client
    now = time.monotonic()
    permit = SimpleNamespace(
        ready=video_ready,
        ready_at=now - 0.02,
        source=SimpleNamespace(session=42),
    )
    client.video_gate = SimpleNamespace(snapshot=lambda: permit)
    client.connection = SimpleNamespace(
        protocol=SimpleNamespace(rpc_ready=True),
        clock=SimpleNamespace(check=lambda: None),
    )
    if sample_kind != "missing":
        client.latest = ControlInput(
            throttle=0.5 if sample_kind == "throttle" else 0,
            brake=0,
            steering=0.5 if sample_kind == "steering" else 0,
            sampled_at=now - (1 if sample_kind == "expired" else 0.05)
            if sample_kind in {"expired", "before_video"}
            else now,
            timestamp_ms=time.time_ns() // 1_000_000,
            valid=sample_kind != "invalid",
        )
    task = asyncio.create_task(client._send_session())
    try:
        assert await asyncio.wait_for(statuses.get(), 1) == expected
        assert not client.armed and client.epoch is None
        if expected == "waiting_device":
            # USB changes refresh the displayed reason even when the client's
            # underlying waiting_input state hasn't changed.
            service._usb_status(True, "已连接")
            assert await asyncio.wait_for(statuses.get(), 1) == "waiting_input"
            service._usb_status(False, "未连接")
            assert await asyncio.wait_for(statuses.get(), 1) == "waiting_device"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_neutral_input_still_acquires_and_waits_for_execution():
    statuses, requests = [], []
    service = DrivingInputService(
        DrivingSettings(session=42),
        on_status=lambda state, detail: statuses.append(state),
    )
    service._usb_status(True, "已连接")
    client = service.client
    permit = SimpleNamespace(
        ready=True,
        ready_at=time.monotonic() - 0.01,
        revision=1,
        source=SimpleNamespace(session=42, principal="vehicle"),
    )
    client.video_gate = SimpleNamespace(snapshot=lambda: permit)
    client.latest = ControlInput(0, 1, 0, time.monotonic(), time.time_ns() // 1_000_000)

    async def request(method, params):
        requests.append(method)
        protocol.rpc_ready = False
        return {
            "epoch": str(uuid4()),
            "session": 42,
            "neutral_required": True,
            "vehicle_principal": "vehicle",
        }

    protocol = SimpleNamespace(rpc_ready=True, request=request)
    client.connection = SimpleNamespace(
        protocol=protocol, clock=SimpleNamespace(check=lambda: None)
    )
    await client._send_session()
    assert requests == ["acquire"]
    assert statuses == ["waiting_execution"]
    assert not client.armed


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
        ]:
            worker._status(state)
            assert control[-1] == (state, text)
            assert received[-1] == (True, '已认证')
        worker._connection_status(False, 'protocol_upgrade_required')
        assert received[-1] == (False, '控制协议不兼容，请同步升级')
    """,
    )

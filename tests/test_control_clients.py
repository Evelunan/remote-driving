import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rd_core.async_compat import async_timeout
from rd_core.control import ControlState
from rd_core.samples import ControlInput
from rd_core.settings import (
    ClientNetworkSettings,
    CredentialSettings,
    DrivingSettings,
    MotionSettings,
    VehicleSettings,
)
from rd_core.video_safety import VideoGate, VideoSource
from rd_core.wire import CONTROL_REVISION, VehicleStatus
from rd_driving.app.control_client import DrivingControlClient
from rd_vehicle.application.control_client import VehicleControlClient
from rd_vehicle.infrastructure.controller.mock_controller import MockController


async def until(predicate, timeout=3):
    async with async_timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_real_feedback_transport_keeps_units_and_never_repeats_stale_cache(
    runtime_factory, client_settings
):
    from rd_vehicle.infrastructure.controller.vehicle_feedback import FeedbackSample

    build, _ = runtime_factory
    runtime = build()
    await runtime.start()
    port = runtime.services["relay"].listener._transport.get_extra_info("sockname")[1]
    vehicle_settings, driving_settings = [
        replace(settings, network=replace(settings.network, quic_port=port))
        for settings in client_settings
    ]
    controller = MockController(vehicle_settings.motion)
    source = SimpleNamespace(sample=None)
    source.latest = lambda: source.sample
    controller.feedback = source
    status = VehicleStatus(3.5, -15, None, 4.5, 2, True, 7, False)
    inputs, reports = Inputs(), []
    vehicle = VehicleControlClient(vehicle_settings, controller)
    driver = DrivingControlClient(driving_settings, inputs.queue,
                                  video_gate=inputs.video_gate,
                                  on_vehicle_status=lambda *args: reports.append(args))
    tasks = [asyncio.create_task(vehicle.run()), asyncio.create_task(inputs.run()),
             asyncio.create_task(driver.run())]
    try:
        await until(lambda: driver.armed)
        source.sample = FeedbackSample(status, time.time_ns() // 1_000_000,
                                       time.monotonic(), 1)
        await until(lambda: len(reports) == 1)
        packet, deadline = reports[0]
        assert packet.payload == status
        assert time.monotonic() < deadline <= time.monotonic() + 0.3
        original_timestamp = packet.source_time_ms
        await asyncio.sleep(0.35)
        assert len(reports) == 1 and driver.armed
        # Changing a revision without a new source timestamp cannot renew freshness.
        source.sample = replace(source.sample, revision=2)
        await asyncio.sleep(0.15)
        assert len(reports) == 1
        source.sample = FeedbackSample(replace(status, speed_mps=0, p_status=True),
                                       time.time_ns() // 1_000_000, time.monotonic(), 3)
        await until(lambda: len(reports) == 2)
        assert reports[-1][0].payload.speed_mps == 0
        assert reports[-1][0].source_time_ms > original_timestamp
        assert reports[-1][0].payload.p_status is True
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()
    assert controller.closed
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "vehicle-feedback"]


class Inputs:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=1)
        self.throttle = 0
        self.steering = 0
        self.brake = 0
        self.enabled = True
        self.sampled = []
        self.video_gate = VideoGate(recovery_ms=1)
        self.video_sequence = 0

    async def run(self):
        while True:
            self.video_sequence += 1
            self.video_gate.observe(
                self.video_sequence,
                time.monotonic(),
                VideoSource("vehicle", 42, "simulated-video"),
                captured_at=time.monotonic(),
            )
            if self.enabled:
                sample = ControlInput(
                    self.throttle,
                    self.brake,
                    self.steering,
                    time.monotonic(),
                    time.time_ns() // 1_000_000,
                )
                if self.queue.full():
                    self.queue.get_nowait()
                self.queue.put_nowait(sample)
                self.sampled.append(sample)
            await asyncio.sleep(0.01)


def test_shared_transport_does_not_implicitly_opt_old_applications_into_video_revision(
    client_settings,
):
    from rd_quic.client import registered_connection

    settings, _ = client_settings
    with pytest.raises(TypeError, match="revision"):
        registered_connection(
            settings.network,
            settings.credentials,
            settings.control,
            session=42,
            role="driving",
        )


@pytest.fixture
def client_settings(local_tls):
    network = ClientNetworkSettings(
        quic_host="127.0.0.1",
        quic_ca_file=local_tls[0],
        quic_server_name="localhost",
        reconnect_delay_s=0.05,
    )
    vehicle = VehicleSettings(
        session=42,
        room="video",
        controller="mock",
        network=network,
        credentials=CredentialSettings("TEST_VEHICLE"),
        motion=MotionSettings(1.2, 0.4),
    )
    driving = DrivingSettings(
        session=42,
        room="video",
        network=network,
        credentials=CredentialSettings("TEST_DRIVER"),
    )
    return vehicle, driving


@pytest.mark.asyncio
async def test_real_clients_execute_configured_targets_expire_input_and_rearm_after_driver_restart(
    runtime_factory, client_settings, monkeypatch
):
    # Input acquisition, vehicle completion and relay use different wall clocks.
    monkeypatch.setattr(
        "rd_vehicle.application.control_client.time",
        SimpleNamespace(
            monotonic=time.monotonic, time_ns=lambda: time.time_ns() + 600_000_000_000
        ),
    )
    monkeypatch.setattr(
        "rd_server.relay.registry.time",
        SimpleNamespace(monotonic=time.monotonic, time=lambda: time.time() - 300),
    )
    build, _ = runtime_factory
    runtime = build()
    relay = runtime.services["relay"]
    await runtime.start()
    port = relay.listener._transport.get_extra_info("sockname")[1]
    vehicle_settings, driving_settings = [
        replace(settings, network=replace(settings.network, quic_port=port))
        for settings in client_settings
    ]
    controller = MockController(vehicle_settings.motion)
    vehicle = VehicleControlClient(vehicle_settings, controller)
    inputs = Inputs()
    reports = []
    driver = DrivingControlClient(
        driving_settings,
        inputs.queue,
        video_gate=inputs.video_gate,
        on_execution=lambda *args: reports.append(args),
    )
    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await until(lambda: driver.armed)
        old_epoch = driver.epoch
        assert vehicle.epoch == old_epoch and reports
        inputs.throttle, inputs.steering = 0.5, -0.5
        await until(lambda: (0.6, -0.2) in controller.commands)
        assert reports[-1][0].timestamp_ms in {
            sample.timestamp_ms for sample in inputs.sampled
        }
        assert reports[-1][2] >= 0
        inputs.enabled = False
        await until(
            lambda: (
                vehicle.supervisor.state == ControlState.FAILSAFE
                and vehicle.epoch is None
            )
        )
        await until(lambda: controller.commands[-1] == (0, 0))
        assert not driver.armed
        inputs.enabled = True
        await asyncio.sleep(0.3)
        assert not driver.armed and vehicle.epoch is None
        inputs.throttle = inputs.steering = 0
        await until(lambda: driver.armed)
        assert driver.epoch != old_epoch
        tasks[-1].cancel()
        await asyncio.gather(tasks[-1], return_exceptions=True)
        await until(lambda: vehicle.epoch is None)
        replacement = DrivingControlClient(
            driving_settings, inputs.queue, video_gate=inputs.video_gate
        )
        tasks[-1] = asyncio.create_task(replacement.run())
        await until(lambda: replacement.armed)
        assert replacement.epoch != old_epoch
        assert vehicle.connection is not None
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results), (
            results
        )
        await runtime.stop()
    assert controller.closed
    assert not relay.registry.bindings and not relay.registry.leases
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name()
        in {
            "rd-clock-monitor",
            "driving-input",
            "vehicle-control-watch",
            "rd-rpc-handler",
        }
    ]
    assert not any(thread.is_alive() for thread in vehicle.supervisor._threads)


@pytest.mark.asyncio
async def test_no_execution_ack_causes_driver_release_and_vehicle_failsafe(
    runtime_factory, client_settings
):
    build, _ = runtime_factory
    runtime = build()
    await runtime.start()
    port = runtime.services["relay"].listener._transport.get_extra_info("sockname")[1]
    vehicle_settings, driving_settings = [
        replace(settings, network=replace(settings.network, quic_port=port))
        for settings in client_settings
    ]
    controller = MockController(vehicle_settings.motion)
    vehicle = VehicleControlClient(vehicle_settings, controller)
    inputs = Inputs()
    statuses = []
    driver = DrivingControlClient(
        driving_settings,
        inputs.queue,
        video_gate=inputs.video_gate,
        on_status=lambda *args: statuses.append(args),
    )
    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await until(lambda: driver.armed)
        inputs.throttle = 0.5
        await until(lambda: controller.commands[-1][0] == 0.6)
        vehicle._result = lambda *args: None
        await until(lambda: ("error", "execution_confirmation_expired") in statuses)
        inputs.enabled = False
        await until(lambda: vehicle.epoch is None and controller.commands[-1] == (0, 0))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()


@pytest.mark.asyncio
async def test_old_connection_callbacks_do_not_touch_current_generation(
    client_settings,
):
    vehicle_settings, driving_settings = client_settings
    driver = DrivingControlClient(driving_settings, asyncio.Queue())
    driver._generation = object()
    driver.state = "active"
    driver._disconnected(object(), None)
    assert driver.state == "active"
    driver._event(object(), "unknown", {})
    vehicle = VehicleControlClient(vehicle_settings, None)
    vehicle._generation = object()
    vehicle._event(object(), "unknown", {})


def test_cached_usb_sample_cannot_move_to_a_later_window(client_settings):
    _, settings = client_settings
    driver = DrivingControlClient(settings, asyncio.Queue())
    driver.connection = SimpleNamespace(max_control_age_ms=300)
    driver._windows.update([(b"a" * 16, 10), (b"b" * 16, 10.15)])
    sample = ControlInput(0.5, 0, 0, 10.1, 123)
    assert driver._sample_window(sample, 10.2) == b"a" * 16
    assert driver._sample_window(sample, 10.31) is None
    assert driver._sample_window(replace(sample, sampled_at=10.16), 10.2) == b"b" * 16


def test_vehicle_discards_latest_command_at_window_deadline(
    client_settings, monkeypatch
):
    from unittest.mock import Mock
    from uuid import uuid4

    from rd_core.wire import Control, Packet

    settings, _ = client_settings
    vehicle = VehicleControlClient(settings, None)
    vehicle.supervisor = Mock()
    vehicle.connection = SimpleNamespace(max_control_age_ms=300)
    vehicle.epoch = uuid4()
    vehicle._lease_deadline = 20
    vehicle._lease_ttl_s = 1
    token = b"x" * 16
    vehicle._windows[token] = 10.3
    packet = Packet(42, vehicle.epoch, 1, 123, Control(0, 0, 0, token))
    now = 10.28
    monkeypatch.setattr(
        "rd_vehicle.application.control_client.time",
        SimpleNamespace(monotonic=lambda: now),
    )
    vehicle._datagram(vehicle._generation, packet.encode())
    assert vehicle.supervisor.submit.call_args.kwargs == {"deadline": 10.3}
    now = 10.3
    vehicle._datagram(vehicle._generation, replace(packet, seq=2).encode())
    assert vehicle.supervisor.submit.call_count == 1
    assert vehicle._last_seq == 1


def test_control_client_imports_do_not_load_gui_media_or_ros(legacy_python):
    legacy_python(
        "vehicle",
        """
        import sys
        from rd_vehicle.application.control_client import VehicleControlClient
        from rd_vehicle.infrastructure.controller.mock_controller import MockController
        from rd_driving.app.control_client import DrivingControlClient
        assert not any(name in sys.modules for name in ('cv2','aiortc','PyQt6','rclpy'))
    """,
    )


@pytest.mark.asyncio
async def test_blocked_actuator_does_not_block_independent_stop_or_invalidate_new_epoch(
    runtime_factory, client_settings
):
    import threading

    build, _ = runtime_factory
    runtime = build()
    await runtime.start()
    port = runtime.services["relay"].listener._transport.get_extra_info("sockname")[1]
    vehicle_settings, driving_settings = [
        replace(settings, network=replace(settings.network, quic_port=port))
        for settings in client_settings
    ]
    entered, release, safety = threading.Event(), threading.Event(), threading.Event()

    class Blocked(MockController):
        def send_command(self, throttle, brake, steering):
            if throttle:
                entered.set()
                release.wait(2)
            super().send_command(throttle, brake, steering)

    controller = Blocked(vehicle_settings.motion)
    vehicle = VehicleControlClient(
        vehicle_settings, controller, independent_stop=lambda reason: safety.set()
    )
    inputs = Inputs()
    driver = DrivingControlClient(
        driving_settings, inputs.queue, video_gate=inputs.video_gate
    )
    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await until(lambda: driver.armed)
        previous = vehicle.epoch
        safety.clear()
        inputs.throttle = 0.5
        await until(entered.is_set)
        await until(
            lambda: (
                safety.is_set() and vehicle.supervisor.state == ControlState.FAILSAFE
            )
        )
        assert not release.is_set()
        inputs.enabled = False
        release.set()
        await until(lambda: vehicle.epoch is None and controller.commands[-1] == (0, 0))
        assert driver.epoch != previous or not driver.armed
        inputs.throttle, inputs.enabled = 0, True
        await until(lambda: driver.armed)
        assert vehicle.epoch != previous
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()


@pytest.mark.asyncio
async def test_client_requires_hostname_verification_but_not_clock_agreement(
    runtime_factory, client_settings
):
    from rd_quic.client import registered_connection

    build, _ = runtime_factory
    runtime = build()
    await runtime.start()
    relay = runtime.services["relay"]
    port = relay.listener._transport.get_extra_info("sockname")[1]
    settings, _ = client_settings
    network = replace(
        settings.network, quic_port=port, quic_server_name="wrong-host.example"
    )
    try:
        with pytest.raises(ConnectionError):
            async with registered_connection(
                network,
                settings.credentials,
                settings.control,
                session=42,
                role="vehicle",
                revision=CONTROL_REVISION,
            ):
                pytest.fail("certificate hostname must be verified")
        network = replace(network, quic_server_name="localhost")
        async with registered_connection(
            network,
            settings.credentials,
            replace(settings.control, max_clock_skew_ms=0),
            session=42,
            role="vehicle",
            revision=CONTROL_REVISION,
        ) as connection:
            assert connection.protocol.rpc_ready
    finally:
        await runtime.stop()
    assert not relay.registry.bindings

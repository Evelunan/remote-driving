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
                                  on_vehicle_status=lambda *args: reports.append(args))
    tasks = [asyncio.create_task(vehicle.run()), asyncio.create_task(driver.run())]
    try:
        await until(lambda: driver.connection is not None and vehicle.connection is not None)
        assert not driver.control_requested and not driver.armed
        source.sample = FeedbackSample(status, time.time_ns() // 1_000_000,
                                       time.monotonic(), 1)
        await until(lambda: len(reports) == 1)
        packet, deadline = reports[0]
        assert packet.payload == status
        assert time.monotonic() < deadline <= time.monotonic() + 0.3
        assert packet.epoch == driver.connection.status_stream
        assert not driver.control_requested and not driver.armed
        original_timestamp = packet.source_time_ms
        tasks.append(asyncio.create_task(inputs.run()))
        assert driver.request_start_control()
        await until(lambda: driver.armed)
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

    async def run(self):
        while True:
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
        on_execution=lambda *args: reports.append(args),
    )
    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await until(lambda: vehicle.connection is not None)
        await until(driver.request_start_control)
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
                not driver.armed and vehicle.epoch != old_epoch
            )
        )
        await until(lambda: controller.commands[-1] == (0, 0))
        assert not driver.armed
        inputs.enabled = True
        await asyncio.sleep(0.3)
        assert not driver.armed and controller.commands[-1] == (0, 0)
        inputs.throttle = inputs.steering = 0
        await until(lambda: driver.armed)
        assert driver.epoch != old_epoch
        tasks[-1].cancel()
        await asyncio.gather(tasks[-1], return_exceptions=True)
        await until(lambda: vehicle.epoch is None)
        replacement = DrivingControlClient(
            driving_settings, inputs.queue
        )
        tasks[-1] = asyncio.create_task(replacement.run())
        await until(replacement.request_start_control)
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
    assert not relay.registry.bindings and not relay.registry.routes
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
@pytest.mark.parametrize("action", ["recover", "cancel_offline", "manual_reconnect"])
async def test_reconnect_respects_control_intent_and_requires_new_neutral(
    runtime_factory, client_settings, action
):
    build, _ = runtime_factory
    runtime = build()
    await runtime.start()
    port = runtime.services["relay"].listener._transport.get_extra_info("sockname")[1]
    vehicle_settings, driving_settings = [
        replace(item, network=replace(item.network, quic_port=port)) for item in client_settings
    ]
    controller = MockController(vehicle_settings.motion)
    vehicle = VehicleControlClient(vehicle_settings, controller)
    inputs, acknowledgements = Inputs(), []
    driver = DrivingControlClient(
        driving_settings, inputs.queue, on_execution=lambda *args: acknowledgements.append(args)
    )
    tasks = [asyncio.create_task(job) for job in (vehicle.run(), inputs.run(), driver.run())]
    try:
        await until(lambda: vehicle.connection is not None)
        await until(driver.request_start_control)
        await until(lambda: driver.armed)
        first = driver.epoch
        inputs.throttle = 0.5
        await until(lambda: controller.commands[-1][0] > 0)
        if action == "manual_reconnect":
            driver.prepare_transport_restart()
            tasks[-1].cancel()
            await asyncio.gather(tasks[-1], return_exceptions=True)
            tasks[-1] = asyncio.create_task(driver.run())
        else:
            await runtime.stop()
            await until(lambda: driver.connection is None and vehicle.epoch is None)
            if action == "cancel_offline":
                assert driver.request_stop_control()
                inputs.throttle = 0
            runtime = build(port=port)
            await runtime.start()
        await until(lambda: driver.connection is not None and vehicle.connection is not None, 8)
        await until(lambda: controller.commands[-1] == (0, 0))
        if action == "cancel_offline":
            await asyncio.sleep(0.4)
            assert driver.state == "idle" and not driver.control_requested
            assert controller.commands[-1] == (0, 0)
        else:
            await until(lambda: driver.state == "waiting_neutral")
            assert driver.control_requested and not driver.armed and controller.commands[-1] == (0, 0)
            inputs.throttle = 0
            await until(lambda: driver.armed)
            assert driver.epoch == vehicle.epoch and driver.epoch != first
            assert acknowledgements[-1][0].throttle == 0
            inputs.throttle = 0.25
            await until(lambda: acknowledgements[-1][0].throttle == 0.25)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()
    assert not driver.control_requested


@pytest.mark.asyncio
async def test_automatic_recovery_ignores_stale_routes_and_stops_on_target_change(client_settings):
    from uuid import uuid4

    _, settings = client_settings
    inputs = Inputs()
    client = DrivingControlClient(settings, inputs.queue)
    epoch, another = uuid4(), uuid4()
    # Start offline. The current driver may control only its original vehicle.
    assert client.request_start_control() and client.state == "waiting_channel"
    def route(revision, epoch, principal):
        client._event(client._generation, "control_route",
                      {"revision": revision, "epoch": str(epoch) if epoch else None,
                       "vehicle_principal": principal})

    route(1, epoch, "original")
    route(3, None, None)
    route(2, another, "new")
    assert client.control_requested and client.epoch is None
    route(4, another, "new")
    assert not client.control_requested and client.state == "vehicle_changed" and not client.armed
    assert client.request_start_control()  # A deliberate new click can select the new target.


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,state,requested", [
    ("access_denied", "paused", False),
    ("protocol_upgrade_required", "paused", False),
    ("driver_already_connected", "channel_busy", True),
])
async def test_registration_refusal_cannot_send_controls(client_settings, monkeypatch, reason, state, requested):
    from contextlib import asynccontextmanager
    from rd_quic.framing import RpcError

    @asynccontextmanager
    async def refuse(*args, **kwargs):
        raise RpcError(reason)
        yield  # Async context manager never enters the registered scope.

    monkeypatch.setattr("rd_driving.app.control_client.registered_connection", refuse)
    _, settings = client_settings
    client = DrivingControlClient(settings, asyncio.Queue())
    assert client.request_start_control()
    task = asyncio.create_task(client.run())
    try:
        await until(lambda: client.state == state)
        assert client.control_requested is requested
        assert not client.armed and client.connection is None and not client._sent
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
        on_status=lambda *args: statuses.append(args),
    )
    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await until(lambda: vehicle.connection is not None)
        await until(driver.request_start_control)
        await until(lambda: driver.armed)
        inputs.throttle = 0.5
        await until(lambda: controller.commands[-1][0] == 0.6)
        vehicle._result = lambda *args: None
        await until(lambda: ("retrying", "execution_confirmation_expired") in statuses)
        inputs.enabled = False
        await until(lambda: not driver.armed and controller.commands[-1] == (0, 0))
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
        driving_settings, inputs.queue
    )
    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(inputs.run()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await until(lambda: vehicle.connection is not None)
        await until(driver.request_start_control)
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
        await until(lambda: not driver.armed and controller.commands[-1] == (0, 0))
        assert driver.epoch != previous or not driver.armed
        inputs.throttle, inputs.enabled = 0, True
        if not driver.control_requested:
            # A concrete REJECTED result from the blocked adapter is terminal.
            await until(driver.request_start_control)
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

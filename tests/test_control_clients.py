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
from rd_core.wire import CONTROL_REVISION
from rd_driving.app.control_client import DrivingControlClient
from rd_quic.client import ClockError, ClockGuard
from rd_vehicle.application.control_client import VehicleControlClient
from rd_vehicle.infrastructure.controller.mock_controller import MockController


async def until(predicate, timeout=3):
    async with async_timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


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
    runtime_factory, client_settings
):
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


@pytest.mark.asyncio
async def test_clock_probe_rejects_skew_and_detects_local_wall_clock_step(monkeypatch):
    class Protocol:
        offset = 0

        async def request(self, method, params):
            return {"server_time_ms": int(time.time() * 1000) + self.offset}

    protocol = Protocol()
    guard = ClockGuard(25)
    await guard.probe(protocol)
    guard.check()
    protocol.offset = 1000
    with pytest.raises(ClockError):
        await guard.probe(protocol)
    wall = time.time_ns()
    monkeypatch.setattr(
        "rd_quic.client.time",
        SimpleNamespace(time_ns=lambda: wall + 1_000_000_000, monotonic=time.monotonic),
    )
    with pytest.raises(ClockError):
        guard.check()


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
async def test_client_requires_hostname_verification_and_compatible_clock_budget(
    runtime_factory, client_settings
):
    from rd_quic.client import registered_connection
    from rd_quic.framing import RpcError

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
        with pytest.raises(RpcError, match="incompatible_clock_budget"):
            async with registered_connection(
                network,
                settings.credentials,
                replace(settings.control, max_clock_skew_ms=20),
                session=42,
                role="vehicle",
                revision=CONTROL_REVISION,
            ):
                pytest.fail("pairwise clock budget must not be weakened locally")
    finally:
        await runtime.stop()
    assert not relay.registry.bindings

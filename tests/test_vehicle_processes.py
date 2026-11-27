import asyncio
import json
import os
import socket
import threading
import time
from dataclasses import replace
from itertools import pairwise

import pytest
from rd_core.async_compat import ExceptionGroup, async_timeout
from rd_core.settings import (
    CameraSettings,
    ClientNetworkSettings,
    CredentialSettings,
    LoggingSettings,
    VehicleSettings,
)
from rd_vehicle.processes import ProcessFailure, VehicleProcesses, _component_lifetime


def test_component_failure_logs_nested_locations_without_payloads(caplog):
    from rd_vehicle.processes import _log_component_failure

    try:
        try:
            raise TimeoutError("sensitive-device-payload")
        except TimeoutError as cause:
            raise RuntimeError("secret-token-value") from cause
    except RuntimeError as failure:
        grouped = ExceptionGroup("private-group-message", [failure, failure])
    _log_component_failure("media", grouped)
    assert "ExceptionGroup" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "test_vehicle_processes.py:" in caplog.text
    assert (
        "test_component_failure_logs_nested_locations_without_payloads" in caplog.text
    )
    assert caplog.text.count("failure detail: RuntimeError") == 1
    for secret in (
        "sensitive-device-payload",
        "secret-token-value",
        "private-group-message",
    ):
        assert secret not in caplog.text


async def recorded_runner(settings, stop):
    """Importable spawn target: use the real component and record exit imports."""
    import sys

    from rd_vehicle.main import run

    component = settings.components[0]
    if component == "media":
        import aioice.ice

        aioice.ice.get_host_addresses = lambda **kwargs: ["127.0.0.1"]
    try:
        await run(settings, stop)
    finally:
        (settings.logging.directory / f"{component}.json").write_text(
            json.dumps({"pid": os.getpid(), "imports": sorted(sys.modules)})
        )


async def blocked_media_runner(settings, stop):
    from rd_core.control import ControlSupervisor
    from rd_vehicle.infrastructure.controller.mock_controller import MockController

    directory = settings.logging.directory
    if settings.components == ("media",):
        if (directory / "media-blocked").exists():
            await stop.wait()
            return
        await asyncio.sleep(1.0)
        (directory / "media-blocked").write_text(str(time.monotonic()))
        time.sleep(5)  # noqa: ASYNC251 - deliberate event-loop stall for fault injection
    else:
        controller = MockController(settings.motion)
        supervisor = ControlSupervisor(controller, timeout_s=0.1)
        ticks = []
        try:
            while not stop.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)
        finally:
            supervisor.stop("process group stopping")
            clean = await asyncio.to_thread(supervisor.close)
            (directory / "control-stopped.json").write_text(
                json.dumps(
                    {
                        "ticks": ticks,
                        "clean": clean,
                        "commands": list(controller.commands),
                        "physical_stop_confirmed": False,
                    }
                )
            )


async def idle_runner(settings, stop):
    await stop.wait()
    await asyncio.sleep(0.1)  # Leave a window to cancel the supervisor again.
    (settings.logging.directory / (settings.components[0] + ".stopped")).touch()
    if settings.room == "cleanup_failure" and settings.components == ("control",):
        raise RuntimeError("injected cleanup failure")


def configuration(tmp_path, local_tls, monkeypatch):
    monkeypatch.setenv("PROCESS_TOKEN", "process-" + "x" * 40)
    return VehicleSettings(
        controller="mock",
        credentials=CredentialSettings("PROCESS_TOKEN"),
        network=ClientNetworkSettings(
            signaling_ca_file=local_tls[0],
            quic_ca_file=local_tls[0],
            reconnect_delay_s=0.05,
        ),
        logging=LoggingSettings(directory=tmp_path / "logs"),
    )


async def until(predicate, task, timeout=20):
    async with async_timeout(timeout):
        while not predicate():
            if task.done():
                task.result()
                raise AssertionError("process group ended before expected state")
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_media_heartbeat_continues_without_opening_camera(
    tmp_path, local_tls, monkeypatch
):
    from rd_vehicle.infrastructure.cameras import TestCamera

    settings = replace(
        configuration(tmp_path, local_tls, monkeypatch),
        components=("media",),
        cameras=(CameraSettings("front", "synthetic", 96, 64, 20),),
        camera_fallback_source="front",
    )
    opened = []

    monkeypatch.setattr(
        TestCamera, "_open", lambda self: opened.append(self.camera_name)
    )
    parent, child = socket.socketpair()
    with parent, child:
        parent.setblocking(False)
        child.setblocking(False)
        task = asyncio.create_task(_component_lifetime(settings, child, runner=None))
        try:
            loop = asyncio.get_running_loop()
            for _ in range(3):
                assert await asyncio.wait_for(loop.sock_recv(parent, 8), 1)
            assert not opened
        finally:
            parent.shutdown(socket.SHUT_WR)
            await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_media_captures_only_while_authenticated_viewers_are_in_the_room(
    runtime_factory, local_tls, tmp_path, monkeypatch
):
    import aioice.ice
    from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
    from rd_vehicle.infrastructure.cameras import TestCamera
    from rd_vehicle.main import run

    monkeypatch.setattr(
        aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
    )
    build, _ = runtime_factory
    server = build(("signaling",), signaling_options={"max_peers_per_room": 3})
    await server.start()
    signaling = server.services["signaling"]
    port = signaling._server.sockets[0].getsockname()[1]
    settings = configuration(tmp_path, local_tls, monkeypatch)
    network = replace(settings.network, signaling_url=f"wss://127.0.0.1:{port}")
    settings = replace(
        settings,
        components=("media",),
        session=42,
        room="video",
        network=network,
        credentials=CredentialSettings("TEST_VEHICLE"),
        cameras=(CameraSettings("front", "synthetic", 96, 64, 30),),
    )
    operations = []
    closing, release = threading.Event(), threading.Event()

    class ObservedCamera(TestCamera):
        def _open(self):
            super()._open()
            operations.append("open")

        def _close(self):
            if not closing.is_set():
                closing.set()
                assert release.wait(5)
            super()._close()
            operations.append("close")

    def camera_factory(camera):
        return ObservedCamera(camera.width, camera.height, camera.fps, camera.name)

    stop = asyncio.Event()
    vehicle = asyncio.create_task(run(settings, stop, camera_factory=camera_factory))
    receivers = []

    def connect(token_env):
        frames = []
        receiver = WebRTCReceiver("video", network, CredentialSettings(token_env))
        receiver.on_frame(lambda name, frame, source: frames.append(name))
        receivers.append((receiver, asyncio.create_task(receiver.start())))
        return receiver, frames

    try:
        await until(lambda: len(signaling.manager.peers) == 1, vehicle)
        publisher = next(iter(signaling.manager.peers))
        assert not operations

        driver, driver_frames = connect("TEST_DRIVER")
        await until(lambda: len(driver_frames) >= 3, vehicle)

        viewer, viewer_frames = connect("TEST_VIEWER")
        await until(lambda: len(viewer_frames) >= 3, vehicle)
        await driver.close()
        await until(lambda: len(signaling.manager.peers) == 2, vehicle)
        count = len(viewer_frames)
        await until(lambda: len(viewer_frames) > count + 2, vehicle)
        assert operations == ["open"]

        await viewer.close()
        await until(closing.is_set, vehicle)
        assert list(signaling.manager.peers) == [publisher]

        reconnected, reconnected_frames = connect("TEST_DRIVER")
        await until(
            lambda: any(
                peer.pc.connectionState == "connected"
                for peer in reconnected.peers.values()
            ),
            vehicle,
        )
        assert operations == ["open"] and not reconnected_frames
        release.set()
        await until(lambda: len(reconnected_frames) >= 3, vehicle)
        assert operations == ["open", "close", "open"]

        await signaling.manager.peers[publisher].websocket.close(
            1012, "test disconnect"
        )
        await until(lambda: publisher not in signaling.manager.peers, vehicle)
        frames_before_reconnect = len(reconnected_frames)
        await until(
            lambda: len(reconnected_frames) > frames_before_reconnect + 2,
            vehicle,
        )
        assert operations == ["open", "close", "open", "close", "open"]
    finally:
        release.set()
        stop.set()
        try:
            await asyncio.gather(*(receiver.close() for receiver, _ in receivers))
            results = await asyncio.gather(
                *(task for _, task in receivers), return_exceptions=True
            )
            assert all(
                result is None or isinstance(result, asyncio.CancelledError)
                for result in results
            )
            await asyncio.wait_for(vehicle, 5)
        finally:
            await server.stop()
    assert operations == ["open", "close", "open", "close", "open", "close"]


@pytest.mark.asyncio
async def test_spawned_control_and_media_execute_receive_video_and_clean_up(
    runtime_factory, local_tls, tmp_path, monkeypatch
):
    import aioice.ice
    from rd_core.samples import ControlInput
    from rd_core.settings import DrivingSettings
    from rd_driving.app.control_client import DrivingControlClient
    from rd_driving.infrastructure.communication import LatestValueQueue
    from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver

    monkeypatch.setattr(
        aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
    )

    build, _ = runtime_factory
    server = build(("relay", "signaling"))
    await server.start()
    settings = configuration(tmp_path, local_tls, monkeypatch)
    network = replace(
        settings.network,
        signaling_url=f"wss://127.0.0.1:{server.services['signaling']._server.sockets[0].getsockname()[1]}",
        quic_host="127.0.0.1",
        quic_server_name="localhost",
        quic_port=server.services["relay"].listener._transport.get_extra_info(
            "sockname"
        )[1],
    )
    settings = replace(
        settings,
        session=42,
        room="video",
        network=network,
        credentials=CredentialSettings("TEST_VEHICLE"),
        cameras=(CameraSettings("front", "synthetic", 96, 64, 20),),
    )
    group = VehicleProcesses(settings, runner=recorded_runner)
    stop = asyncio.Event()
    group_task = asyncio.create_task(group.run(stop))
    driving = DrivingSettings(
        session=42,
        room="video",
        network=network,
        credentials=CredentialSettings("TEST_DRIVER"),
    )
    samples = LatestValueQueue()
    executed = []
    client = DrivingControlClient(
        driving,
        samples,
        on_execution=lambda *args: executed.append(args[0]),
    )
    receiver = WebRTCReceiver("video", network, driving.credentials)
    frames = []

    def received(name, pixels, source):
        frames.append((name, pixels.shape))

    receiver.on_frame(received)
    axis = 0.2

    async def produce():
        while True:
            await samples.put(
                ControlInput(axis, 0, 0, time.monotonic(), time.time_ns() // 1_000_000)
            )
            await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(job) for job in (client.run(), produce())]
    try:
        await until(
            lambda: (
                client.connection is not None
                and 42 in server.services["relay"].registry.vehicles
            ),
            group_task,
        )
        assert client.request_start_control()
        await until(lambda: client.armed, group_task, timeout=12)
        await until(
            lambda: any(sample.throttle == 0.2 for sample in executed), group_task
        )
        epoch = client.epoch
        tasks.append(asyncio.create_task(receiver.start()))
        await until(lambda: frames, group_task, timeout=12)
        assert frames[-1] == ("front", (64, 96, 3))
        assert client.armed and client.epoch == epoch
        assert client.request_stop_control()
        await until(lambda: not client.armed and client.epoch != epoch, group_task)
        frame_count = len(frames)
        await until(lambda: len(frames) >= frame_count + 3, group_task)
        axis = 0.0
        await until(client.request_start_control, group_task)
        await until(lambda: client.armed, group_task)
        assert client.epoch != epoch
        epoch = client.epoch
        await receiver.close()
        axis = 0.3
        await until(
            lambda: any(sample.throttle == 0.3 for sample in executed), group_task
        )
        assert client.armed and client.epoch == epoch
        stop.set()
        await asyncio.wait_for(group_task, 10)
    finally:
        stop.set()
        await asyncio.gather(group_task, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await receiver.close()
        await server.stop()
    assert len(group.outcomes) == 2
    assert all(item["exitcode"] == 0 and not item["forced"] for item in group.outcomes)
    assert len({item["pid"] for item in group.outcomes} | {os.getpid()}) == 3
    control = json.loads((settings.logging.directory / "control.json").read_text())
    media = json.loads((settings.logging.directory / "media.json").read_text())
    assert not {
        "cv2",
        "aiortc",
        "av",
        "PyQt6",
        "numpy",
        "rd_vehicle.interfaces.icamera",
    } & set(control["imports"])
    assert "aiortc" in media["imports"]


@pytest.mark.asyncio
async def test_media_stall_does_not_stall_control_and_forced_exit_is_reported(
    tmp_path, local_tls, monkeypatch
):
    settings = configuration(tmp_path, local_tls, monkeypatch)
    # Shorten only the fault being injected. Child imports and the first
    # heartbeat use the same startup budget as production on a busy host.
    settings = replace(
        settings,
        processes=replace(
            settings.processes, heartbeat_timeout_s=0.5, shutdown_timeout_s=1.0
        ),
    )
    group = VehicleProcesses(settings, runner=blocked_media_runner)
    stop = asyncio.Event()
    task = asyncio.create_task(group.run(stop))
    try:
        await until(
            lambda: (
                any(
                    item["component"] == "media" and item["forced"]
                    for item in group.outcomes
                )
                and len(group.children) == 2
                and all(child.heartbeat_at for child in group.children)
            ),
            task,
            timeout=30,
        )
        assert not (settings.logging.directory / "control-stopped.json").exists()
        restarted = next(
            child.started_at for child in group.children if child.component == "media"
        )
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    report = json.loads(
        (settings.logging.directory / "control-stopped.json").read_text()
    )
    blocked = float((settings.logging.directory / "media-blocked").read_text())
    assert report["clean"] and report["commands"][-1] == [0, 0]
    assert not report["physical_stop_confirmed"]
    # The independent control loop continues after the media loop blocks.
    assert sum(blocked <= stamp < restarted for stamp in report["ticks"]) >= 3
    assert [
        item["forced"] for item in group.outcomes if item["component"] == "media"
    ] == [True, False]
    control = [item for item in group.outcomes if item["component"] == "control"]
    assert (
        len(control) == 1 and not control[0]["forced"] and control[0]["exitcode"] == 0
    )
    assert all(item["exitcode"] is not None for item in group.outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["control", "media"])
@pytest.mark.parametrize("cause", ["crash", "parent_channel_lost"])
async def test_exactly_one_child_restarts_and_sibling_keeps_running(
    component, cause, tmp_path, local_tls, monkeypatch
):
    settings = configuration(tmp_path, local_tls, monkeypatch)
    group = VehicleProcesses(settings, runner=idle_runner)
    stop = asyncio.Event()
    task = asyncio.create_task(group.run(stop))
    try:
        await until(
            lambda: (
                len(group.children) == 2 and all(c.heartbeat_at for c in group.children)
            ),
            task,
        )
        children = {child.component: child for child in group.children}
        failed = children[component]
        sibling = next(child for child in group.children if child is not failed)
        heartbeat, pid = sibling.heartbeat_at, failed.process.pid
        if cause == "crash":
            failed.process.kill()
        else:
            # A supervising parent that disappears delivers this EOF to each child.
            failed.channel.shutdown(socket.SHUT_WR)
        await until(
            lambda: any(
                c.component == component and c.process.pid != pid and c.heartbeat_at
                for c in group.children
            ),
            task,
            timeout=20,
        )
        assert sibling in group.children and sibling.heartbeat_at > heartbeat
        assert not (
            settings.logging.directory / (sibling.component + ".stopped")
        ).exists()
        if cause == "parent_channel_lost":
            assert (settings.logging.directory / (component + ".stopped")).exists()
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert len(group.outcomes) == 3
    assert len({item["pid"] for item in group.outcomes}) == 3
    assert all(item["exitcode"] is not None for item in group.outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["cancel", "cleanup_failure"])
async def test_process_group_cleanup_on_cancellation_and_shutdown_failure(
    cause, tmp_path, local_tls, monkeypatch
):
    settings = configuration(tmp_path, local_tls, monkeypatch)
    if cause == "cleanup_failure":
        settings = replace(settings, room="cleanup_failure")
    group = VehicleProcesses(settings, runner=idle_runner)
    stop = asyncio.Event()
    task = asyncio.create_task(group.run(stop))
    try:
        await until(
            lambda: (
                len(group.children) == 2
                and all(child.heartbeat_at for child in group.children)
            ),
            task,
        )
        if cause == "cancel":
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stop.set()
            with pytest.raises(ProcessFailure):
                await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(group.outcomes) == 2
    assert not any(
        item["forced"] or item["exitcode"] is None for item in group.outcomes
    )
    assert (settings.logging.directory / "control.stopped").exists()
    assert (settings.logging.directory / "media.stopped").exists()


@pytest.mark.asyncio
async def test_partial_spawn_failure_retries_child_and_prestart_stop_opens_none(
    tmp_path, local_tls, monkeypatch
):
    from multiprocessing.process import BaseProcess

    # The first spawned interpreter may still be importing when the second spawn fails.
    settings = configuration(tmp_path, local_tls, monkeypatch)
    original = BaseProcess.start
    attempts = []

    def start(process):
        if process.name == "rd-vehicle-media":
            attempts.append(time.monotonic())
            if len(attempts) <= 2:
                raise OSError("injected second process spawn failure")
        return original(process)

    monkeypatch.setattr(BaseProcess, "start", start)
    group = VehicleProcesses(settings, runner=idle_runner)
    stop = asyncio.Event()
    task = asyncio.create_task(group.run(stop))
    try:
        await until(
            lambda: (
                len(group.children) == 2 and all(c.heartbeat_at for c in group.children)
            ),
            task,
            timeout=12,
        )
        assert not group.outcomes
        assert len(attempts) == 3
        assert min(b - a for a, b in pairwise(attempts)) >= 0.2
    finally:
        stop.set()
        await asyncio.wait_for(task, 7)
    assert len(group.outcomes) == 2
    assert all(item["exitcode"] == 0 and not item["forced"] for item in group.outcomes)
    stopped = asyncio.Event()
    stopped.set()
    group = VehicleProcesses(settings, runner=idle_runner)
    await group.run(stopped)
    assert not group.outcomes

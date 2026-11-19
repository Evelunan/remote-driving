import asyncio
import json
import os
import time
from dataclasses import replace

import pytest
from rd_core.async_compat import ExceptionGroup, async_timeout
from rd_core.settings import (
    CameraSettings,
    ClientNetworkSettings,
    CredentialSettings,
    LoggingSettings,
    ProcessSettings,
    VehicleSettings,
)
from rd_core.video_safety import VideoGate
from rd_vehicle.processes import ProcessFailure, VehicleProcesses


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
        processes=ProcessSettings(
            heartbeat_interval_s=0.02,
            heartbeat_timeout_s=0.3,
            startup_timeout_s=0.8,
            shutdown_timeout_s=0.5,
            kill_timeout_s=0.3,
        ),
    )


async def until(predicate, task, timeout=6):
    async with async_timeout(timeout):
        while not predicate():
            if task.done():
                task.result()
                raise AssertionError("process group ended before expected state")
            await asyncio.sleep(0.02)


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
        processes=replace(
            settings.processes,
            heartbeat_timeout_s=1,
            startup_timeout_s=8,
            shutdown_timeout_s=4,
        ),
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
    gate = VideoGate()
    client = DrivingControlClient(
        driving,
        samples,
        video_gate=gate,
        on_execution=lambda *args: executed.append(args[0]),
    )
    receiver = WebRTCReceiver("video", network, driving.credentials)
    frames = []

    def received(name, pixels, source):
        frames.append((name, pixels.shape))
        if name == "front":
            gate.observe(len(frames), time.monotonic(), source)

    receiver.on_frame(received)
    axis = 0.0

    async def produce():
        while True:
            await samples.put(
                ControlInput(axis, 0, 0, time.monotonic(), time.time_ns() // 1_000_000)
            )
            await asyncio.sleep(0.01)

    tasks = [
        asyncio.create_task(job) for job in (client.run(), receiver.start(), produce())
    ]
    try:
        await until(lambda: client.armed and frames, group_task, timeout=12)
        assert frames[-1] == ("front", (64, 96, 3))
        axis = 0.2
        await until(
            lambda: any(sample.throttle == 0.2 for sample in executed), group_task
        )
        stop.set()
        await asyncio.wait_for(group_task, 6)
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
    group = VehicleProcesses(settings, runner=blocked_media_runner)
    with pytest.raises(ProcessFailure):
        await asyncio.wait_for(group.run(), 7)
    report = json.loads(
        (settings.logging.directory / "control-stopped.json").read_text()
    )
    blocked = float((settings.logging.directory / "media-blocked").read_text())
    assert report["clean"] and report["commands"][-1] == [0, 0]
    assert not report["physical_stop_confirmed"]
    # The independent control loop continues after the media loop blocks.
    after = [stamp for stamp in report["ticks"] if stamp >= blocked]
    assert len(after) >= 3
    assert max(b - a for a, b in zip(report["ticks"], report["ticks"][1:])) < 0.2
    outcomes = {item["component"]: item for item in group.outcomes}
    assert outcomes["media"]["forced"]
    assert not outcomes["control"]["forced"] and outcomes["control"]["exitcode"] == 0
    assert all(item["exitcode"] is not None for item in group.outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause", ["crash", "cancel", "parent_channel_lost", "cleanup_failure"]
)
async def test_process_group_cleanup_on_crash_cancellation_and_parent_loss(
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
        children = {child.component: child for child in group.children}
        if cause == "crash":
            children["media"].process.kill()
            with pytest.raises(ProcessFailure):
                await task
        elif cause == "cancel":
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif cause == "cleanup_failure":
            stop.set()
            with pytest.raises(ProcessFailure):
                await task
        else:
            # Parent death closes its sockets. Deliver the same EOF to control;
            # it must clean itself up without a signal or another IPC message.
            children["control"].channel.shutdown(1)
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
    if cause != "crash":
        assert (settings.logging.directory / "media.stopped").exists()


@pytest.mark.asyncio
async def test_partial_spawn_failure_reaps_started_child_and_prestart_stop_opens_none(
    tmp_path, local_tls, monkeypatch
):
    from multiprocessing.process import BaseProcess

    settings = configuration(tmp_path, local_tls, monkeypatch)
    original = BaseProcess.start

    def start(process):
        if process.name == "rd-vehicle-media":
            raise OSError("injected second process spawn failure")
        return original(process)

    monkeypatch.setattr(BaseProcess, "start", start)
    group = VehicleProcesses(settings, runner=idle_runner)
    with pytest.raises(OSError, match="injected"):
        await group.run()
    assert len(group.outcomes) == 1
    assert group.outcomes[0]["exitcode"] == 0 and not group.outcomes[0]["forced"]
    stopped = asyncio.Event()
    stopped.set()
    group = VehicleProcesses(settings, runner=idle_runner)
    await group.run(stopped)
    assert not group.outcomes

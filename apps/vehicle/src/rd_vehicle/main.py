"""Vehicle entry: one validated configuration for control, media and devices."""

import asyncio
from contextlib import ExitStack

from rd_core.async_compat import ExceptionGroup, TaskGroup
from rd_core.cli import client_arguments
from rd_core.configuration import ConfigError
from rd_core.logging import configure_logging, get_logger
from rd_core.settings import VehicleSettings

from rd_vehicle.shutdown import stop_on_signals

logger = get_logger(__name__)


def parse_args(argv=None):
    return client_arguments(VehicleSettings, "vehicle", argv)


def active_components(settings):
    return tuple(
        name
        for name in settings.components
        if name != "control" or settings.controller != "none"
    )


def check_config(settings):
    settings.validate()
    components = active_components(settings)
    if "control" in components:
        from rd_quic.client import check_control_config

        check_control_config(settings.network, settings.credentials)
        if settings.controller == "ros2":
            from rd_vehicle.infrastructure.controller.ros2_cmd_vel_controller import (
                RCLpyCmdVelController,  # noqa: F401
            )
        elif settings.controller == "serial":
            if (
                max(settings.motion.max_linear_mps, settings.motion.max_angular_radps)
                > 655.35
            ):
                raise ConfigError("motion limit exceeds serial uint16 representation")
            import serial  # noqa: F401
    if "media" in components:
        from rd_webrtc.signaling import check_media_config

        check_media_config(settings.network, settings.credentials)
        if settings.encoder_workers is not None:
            from rd_webrtc.encoding import check_encoding_runtime

            check_encoding_runtime()


def _create_controller(settings):
    match settings.controller:
        case "none":
            return None
        case "mock":
            from rd_vehicle.infrastructure.controller.mock_controller import (
                MockController,
            )

            return MockController(settings.motion)
        case "serial":
            from rd_vehicle.infrastructure.controller.serial_controller import (
                SerialController,
            )

            return SerialController(settings)
        case "ros2":
            from rd_vehicle.infrastructure.controller.ros2_cmd_vel_controller import (
                RCLpyCmdVelController,
            )

            return RCLpyCmdVelController(
                max_linear=settings.motion.max_linear_mps,
                max_angular=settings.motion.max_angular_radps,
            )
        case _:
            raise ValueError("unknown controller")


def _create_camera(settings):
    from rd_vehicle.infrastructure.cameras import PyAVCamera, TestCamera, USBCamera

    if settings.source is None or settings.source == "synthetic":
        return TestCamera(
            width=settings.width,
            height=settings.height,
            fps=settings.fps,
            name=settings.name,
        )
    adapter = PyAVCamera if settings.capture_backend == "pyav" else USBCamera
    if adapter is PyAVCamera and not PyAVCamera.supports(settings.source):
        raise ConfigError("pyav capture requires a Linux /dev video source")
    return adapter(
        settings.source,
        settings.name,
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
    )


async def start_control(settings, *, controller_factory=None, capture_health=None):
    from rd_vehicle.application.control_client import VehicleControlClient

    controller = (controller_factory or _create_controller)(settings)
    if controller is None:
        raise ConfigError("control component requires a controller")
    client = VehicleControlClient(settings, controller, capture_health=capture_health)
    await client.run()


def _configure_cameras(manager, settings, camera_factory):
    if settings.camera_fallback_source is None:
        for camera in settings.cameras:
            manager.add_camera(camera_factory(camera), encoding=camera.encoding)
        return

    cameras = {camera.name: camera for camera in settings.cameras}
    source = cameras[settings.camera_fallback_source]
    for camera in settings.cameras:
        if camera.source is not None:
            manager.add_camera(camera_factory(camera), encoding=camera.encoding)
    copies = []
    for name in ("front", "back", "left", "right"):
        if name not in cameras or cameras[name].source is None:
            manager.add_camera_copy(
                name, source.name, encoding=cameras.get(name, source).encoding
            )
            copies.append(name)
    if copies:
        logger.info("Camera test fallback: %s supplies views %s", source.name, copies)


async def start_streaming(
    settings,
    *,
    camera_factory=None,
    on_fault=None,
    on_started=None,
    capture_health=None,
):
    from rd_vehicle.application.multi_camera_manager import MultiCameraManager
    from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer

    streamer = WebRTCStreamer(
        settings.room,
        settings.network,
        settings.credentials,
        session=settings.session,
        encoder_workers=settings.encoder_workers,
    )
    manager = MultiCameraManager(
        streamer,
        video_policy=settings.video,
        on_fault=on_fault,
        on_capture_health=capture_health.update if capture_health is not None else None,
    )
    try:
        _configure_cameras(manager, settings, camera_factory or _create_camera)
        await manager.start()
        if on_started is not None:
            on_started()
        async with TaskGroup() as tasks:
            tasks.create_task(streamer.start())
            tasks.create_task(manager.wait())
    finally:
        try:
            await manager.stop()
        finally:
            await streamer.close()


async def run(
    settings,
    stop=None,
    *,
    controller_factory=None,
    camera_factory=None,
    install_signals=False,
    on_fault=None,
    on_media_started=None,
    capture_health=None,
):
    """In-process component host; the production CLI owns separate spawned hosts."""
    check_config(settings)
    stop = stop or asyncio.Event()
    if stop.is_set():
        return
    signals = ExitStack()
    if install_signals:
        signals.enter_context(stop_on_signals(stop))
    tasks = []
    stopped = None
    try:
        components = active_components(settings)
        if "media" in components and capture_health is None:
            from rd_vehicle.application.capture_health import CaptureHealth

            capture_health = CaptureHealth()
        if "control" in components:
            tasks.append(
                asyncio.create_task(
                    start_control(
                        settings,
                        controller_factory=controller_factory,
                        capture_health=capture_health,
                    ),
                    name="vehicle-control",
                )
            )
        else:
            logger.info(
                "Vehicle control disabled; no control registration or device opened"
            )
        if "media" in components:
            tasks.append(
                asyncio.create_task(
                    start_streaming(
                        settings,
                        camera_factory=camera_factory,
                        on_fault=on_fault,
                        on_started=on_media_started,
                        capture_health=capture_health,
                    ),
                    name="vehicle-media",
                )
            )
        stopped = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            [*tasks, stopped], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not stopped:
                task.result()
                raise RuntimeError("vehicle component ended unexpectedly")
    except Exception:
        if on_fault is not None:
            on_fault()
        raise
    finally:
        if stopped is not None:
            stopped.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            results = await asyncio.gather(
                *tasks,
                *([stopped] if stopped is not None else []),
                return_exceptions=True,
            )
        finally:
            signals.close()
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("vehicle component failed", failures)


def cli(argv=None):
    args = parse_args(argv)
    if args.dump_config:
        print(args.loaded.as_json())
        return 0
    settings = args.loaded.value
    configure_logging(settings.logging.level, directory=settings.logging.directory)
    try:
        if args.check_config:
            check_config(settings)
            print(
                "Configuration, credentials, TLS and selected controller imports are valid; no devices opened."
            )
        else:
            from rd_vehicle.processes import VehicleProcesses

            asyncio.run(VehicleProcesses(settings).run(install_signals=True))
    except KeyboardInterrupt:
        pass
    except (ConfigError, OSError, ImportError, ValueError) as exc:
        logger.error("Vehicle startup failed (%s)", type(exc).__name__)
        if isinstance(exc, ConfigError):
            logger.error("%s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001 - application boundary, no raw device/payload output
        logger.error("Vehicle runtime failed (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())

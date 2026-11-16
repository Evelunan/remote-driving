"""Vehicle entry: one validated configuration for control, media and devices."""

import asyncio
import signal

from rd_core.cli import client_arguments
from rd_core.configuration import ConfigError
from rd_core.logging import configure_logging, get_logger
from rd_core.settings import VehicleSettings

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
    from rd_vehicle.infrastructure.cameras import TestCamera, USBCamera

    if settings.source == "synthetic":
        return TestCamera(
            width=settings.width,
            height=settings.height,
            fps=settings.fps,
            name=settings.name,
        )
    return USBCamera(
        settings.source,
        settings.name,
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
    )


async def start_control(settings, *, controller_factory=None):
    from rd_vehicle.application.control_client import VehicleControlClient

    controller = (controller_factory or _create_controller)(settings)
    if controller is None:
        raise ConfigError("control component requires a controller")
    client = VehicleControlClient(settings, controller)
    await client.run()


async def start_streaming(settings, *, camera_factory=None):
    from rd_vehicle.application.multi_camera_manager import MultiCameraManager
    from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer

    streamer = WebRTCStreamer(settings.room, settings.network, settings.credentials)
    manager = MultiCameraManager(streamer)
    try:
        for camera_settings in settings.cameras:
            manager.add_camera((camera_factory or _create_camera)(camera_settings))
        await manager.start()
        async with asyncio.TaskGroup() as tasks:
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
):
    check_config(settings)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = []
    for sig in (signal.SIGINT, signal.SIGTERM) if install_signals else ():
        try:
            loop.add_signal_handler(sig, stop.set)
            signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    tasks = []
    stopped = None
    try:
        components = active_components(settings)
        if "control" in components:
            tasks.append(
                asyncio.create_task(
                    start_control(settings, controller_factory=controller_factory),
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
                    start_streaming(settings, camera_factory=camera_factory),
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
    finally:
        if stopped is not None:
            stopped.cancel()
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        results = await asyncio.gather(
            *tasks, *([stopped] if stopped is not None else []), return_exceptions=True
        )
        for sig in signals:
            loop.remove_signal_handler(sig)
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
            asyncio.run(run(settings, install_signals=True))
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

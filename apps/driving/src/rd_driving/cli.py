"""Headless configuration entry; importing it does not load Qt or OpenCV."""

from rd_core.cli import client_arguments
from rd_core.configuration import ConfigError
from rd_core.logging import configure_logging, get_logger
from rd_core.settings import DrivingSettings

logger = get_logger(__name__)


def parse_args(argv=None):
    return client_arguments(DrivingSettings, "driving", argv)


def check_config(settings):
    from rd_quic.client import check_control_config
    from rd_webrtc.signaling import check_media_config

    settings.validate()
    check_control_config(settings.network, settings.credentials)
    check_media_config(settings.network, settings.credentials)


def main(argv=None):
    args = parse_args(argv)
    if args.dump_config:
        print(args.loaded.as_json())
        return 0
    settings = args.loaded.value
    configure_logging(settings.logging.level, directory=settings.logging.directory)
    try:
        check_config(settings)
        if args.check_config:
            print(
                "Configuration, credentials and TLS are valid; no devices or GUI opened."
            )
            return 0
        from rd_driving.main_window import run_gui

        connect = args.connect or (args.session is not None and args.room is not None)
        return run_gui(settings, connect=connect)
    except (ConfigError, OSError, ImportError, ValueError) as exc:
        logger.error("Driving startup failed (%s)", type(exc).__name__)
        if isinstance(exc, ConfigError):
            logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""One configuration entry for independently selectable server modules."""

import argparse
import asyncio
import signal
import ssl
from pathlib import Path

from aioquic.quic.configuration import QuicConfiguration
from rd_core.auth import AuthProvider
from rd_core.configuration import ConfigError, cli_overrides, load_config
from rd_core.logging import configure_logging, get_logger
from rd_core.settings import ServerSettings

from .relay.service import RelayService
from .runtime import ServerRuntime
from .signaling.server import SignalingServer

logger = get_logger(__name__)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Remote-driving signaling and QUIC relay"
    )
    parser.add_argument(
        "--config", type=Path, help="instance YAML; otherwise RD_CONFIG"
    )
    parser.add_argument(
        "--set",
        action="append",
        metavar="FIELD=VALUE",
        help="typed field override (repeatable)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-config",
        action="store_true",
        help="validate settings, credentials and TLS without listening",
    )
    modes.add_argument(
        "--dump-config",
        action="store_true",
        help="show effective settings and sources, without resolving secrets",
    )
    parser.add_argument("--services", help="signaling,relay or either module")
    parser.add_argument("--host", help="set both listener addresses")
    parser.add_argument("--signaling-port", type=int)
    parser.add_argument("--relay-port", type=int)
    parser.add_argument("--cert", type=Path, help="set both TLS certificate paths")
    parser.add_argument("--key", type=Path, help="set both TLS private-key paths")
    parser.add_argument(
        "--allow-legacy-development",
        action="store_true",
        default=None,
        help="enable unauthenticated v1 protocol on loopback only",
    )
    args = parser.parse_args(argv)
    overrides = {}
    for name, value in (
        ("host", args.host),
        ("tls.cert", args.cert),
        ("tls.key", args.key),
    ):
        if value is not None:
            for module in ("signaling", "relay"):
                overrides[f"{module}.{name}"] = value
    for name, value in (
        ("signaling.port", args.signaling_port),
        ("relay.port", args.relay_port),
        ("allow_legacy_development", args.allow_legacy_development),
    ):
        if value is not None:
            overrides[name] = value
    if args.services is not None:
        overrides["services"] = args.services.split(",")
    try:
        explicit = cli_overrides(args.set)
        if set(explicit) & set(overrides):
            raise ConfigError(
                "do not set the same field through both a convenience flag and --set"
            )
        overrides.update(explicit)
        args.loaded = load_config(
            ServerSettings, "server", path=args.config, overrides=overrides
        )
    except ConfigError as error:
        parser.error(str(error))
    return args


def create_runtime(args):
    settings = args.loaded.value
    if "relay" in settings.services and not settings.allow_legacy_development:
        raise ConfigError(
            "authenticated QUIC v2 is pending; relay currently requires explicit loopback legacy mode"
        )
    auth = None if settings.allow_legacy_development else AuthProvider(settings.auth)
    services = {}
    for name in settings.services:
        config = getattr(settings, name)
        if name == "signaling":
            tls = None
            if config.tls.cert is not None:
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.minimum_version = ssl.TLSVersion.TLSv1_2
                tls.load_cert_chain(config.tls.cert, config.tls.key)
            services[name] = SignalingServer(
                config,
                tls_context=tls,
                auth=auth,
                allow_legacy_development=settings.allow_legacy_development,
            )
        else:
            configuration = QuicConfiguration(
                is_client=False,
                max_datagram_frame_size=config.max_datagram_bytes,
                idle_timeout=config.idle_timeout_s,
            )
            configuration.load_cert_chain(config.tls.cert, config.tls.key)
            services[name] = RelayService(config.host, config.port, configuration)
    return ServerRuntime(services)


async def run(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        except NotImplementedError:
            pass
    try:
        runtime = create_runtime(args)
        logger.info("Starting modules: %s", ",".join(args.loaded.value.services))
        await runtime.run(stop)
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


def main(argv=None):
    args = parse_args(argv)
    if args.dump_config:
        print(args.loaded.as_json())
        return 0
    settings = args.loaded.value.logging
    configure_logging(settings.level, directory=settings.directory)
    try:
        if args.check_config:
            create_runtime(args)
            print("Configuration, credentials and TLS are valid; no listeners started.")
        else:
            asyncio.run(run(args))
    except (ConfigError, OSError, ValueError) as error:
        # TLS/parser errors may include source text; never print credential content.
        logger.error("Server startup failed (%s)", type(error).__name__)
        if isinstance(error, ConfigError):
            logger.error("%s", error)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

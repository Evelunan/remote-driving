"""Unified listener entry; legacy protocol is restricted to explicit local tests."""

import argparse
import asyncio
import signal
import ssl
from pathlib import Path

from aioquic.quic.configuration import QuicConfiguration
from rd_core.logging import configure_logging, get_logger

from .relay.service import RelayService
from .runtime import ServerRuntime
from .signaling.config.loaders.server_loader import ServerConfig
from .signaling.server import SignalingServer

logger = get_logger(__name__)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Remote-driving signaling and QUIC relay"
    )
    parser.add_argument(
        "--services", default="signaling,relay", help="signaling,relay or either module"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--signaling-port", type=int, default=8765)
    parser.add_argument("--relay-port", type=int, default=4433)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument(
        "--allow-legacy-development",
        action="store_true",
        help="enable unauthenticated v1 protocol on loopback only",
    )
    args = parser.parse_args(argv)
    args.services = tuple(args.services.split(","))
    if len(set(args.services)) != len(args.services) or not set(args.services) <= {
        "signaling",
        "relay",
    }:
        parser.error("services must be signaling, relay, or signaling,relay")
    if not args.allow_legacy_development or args.host not in {"127.0.0.1", "::1"}:
        parser.error(
            "v1 is only available with --allow-legacy-development on loopback; authenticated v2 is pending"
        )
    if not all(0 <= port <= 65535 for port in (args.signaling_port, args.relay_port)):
        parser.error("invalid listener port")
    if bool(args.cert) != bool(args.key) or (
        "relay" in args.services and args.cert is None
    ):
        parser.error("relay requires --cert and --key; supply both TLS paths together")
    return args


def create_runtime(args):
    services = {}
    tls = None
    if args.cert is not None:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
    for name in args.services:
        if name == "signaling":
            services[name] = SignalingServer(
                ServerConfig(args.host, args.signaling_port, 2, 20, 20, 65536),
                tls_context=tls,
            )
        else:
            configuration = QuicConfiguration(
                is_client=False, max_datagram_frame_size=65536
            )
            configuration.load_cert_chain(args.cert, args.key)
            services[name] = RelayService(args.host, args.relay_port, configuration)
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
        logger.info("Starting modules: %s", ",".join(args.services))
        await runtime.run(stop)
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


def main():
    args = parse_args()
    configure_logging()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

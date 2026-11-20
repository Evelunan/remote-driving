"""Verified client connection and revision-checked registration."""

import asyncio
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from rd_core.async_compat import async_timeout
from rd_core.auth import read_secret
from rd_core.configuration import ConfigError
from rd_core.logging import get_logger
from rd_core.settings import ClientNetworkSettings, ControlSettings, CredentialSettings
from rd_core.wire import ALPN, CONTROL_REVISION

from .framing import RpcError
from .protocol import RpcProtocol

logger = get_logger(__name__)


def quic_configuration(network: ClientNetworkSettings):
    return QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
        server_name=network.quic_server_name,
        cafile=str(network.quic_ca_file) if network.quic_ca_file else None,
        verify_mode=ssl.CERT_REQUIRED,
        max_datagram_frame_size=1200,
        idle_timeout=network.idle_timeout_s,
    )


def check_control_config(network, credentials):
    """Resolve local TLS trust and token without opening a socket or device."""
    token = read_secret(credentials.token_env)
    if not 32 <= len(token) <= 4096:
        raise ConfigError("control token must have 32..4096 characters")
    ssl.create_default_context(cafile=network.quic_ca_file)
    quic_configuration(network)


@dataclass(frozen=True)
class ControlConnection:
    protocol: RpcProtocol
    max_control_age_ms: int
    max_clock_skew_ms: int


@asynccontextmanager
async def registered_connection(
    network: ClientNetworkSettings,
    credentials: CredentialSettings,
    control: ControlSettings,
    *,
    session: int,
    role: str,
    revision: str,
    request_handler=None,
    event_handler=None,
    datagram_handler=None,
    on_disconnect=None,
):
    if revision != CONTROL_REVISION:
        raise RpcError("protocol_upgrade_required")
    token = read_secret(credentials.token_env)
    configuration = quic_configuration(network)
    protocol = None

    def factory(*args, **kwargs):
        nonlocal protocol
        protocol = RpcProtocol(
            *args,
            request_handler=request_handler,
            event_handler=event_handler,
            datagram_handler=datagram_handler,
            on_disconnect=on_disconnect,
            rpc_timeout_s=network.register_timeout_s,
            **kwargs,
        )
        return protocol

    manager = connect(
        network.quic_host,
        network.quic_port,
        configuration=configuration,
        create_protocol=factory,
        wait_connected=False,
    )
    entered = False
    handshake = None
    try:
        async with async_timeout(network.register_timeout_s):
            await manager.__aenter__()
            entered = True
            protocol.transmit()
            # aioquic shields its internal Future. Keep its awaiting task owned
            # until shutdown, even if registration times out or is cancelled.
            handshake = asyncio.create_task(protocol.wait_connected())
            await asyncio.shield(handshake)
            result = await protocol.request(
                "register",
                {
                    "token": token,
                    "session": session,
                    "role": role,
                    "revision": revision,
                },
            )
            if result.get("revision") != CONTROL_REVISION:
                raise RpcError("protocol_upgrade_required")
            if result.get("session") != session or result.get("role") != role:
                raise RpcError("registration_mismatch")
            age, skew = (
                result.get("max_control_age_ms"),
                result.get("max_clock_skew_ms"),
            )
            if type(age) is not int or type(skew) is not int or not 0 <= skew < age:
                raise RpcError("invalid_server_limits")
            age = min(age, control.command_timeout_ms)
        logger.info(
            "Control relay registered role=%s session=%s relay=%s:%s revision=%s",
            role,
            session,
            network.quic_host,
            network.quic_port,
            revision,
        )
        try:
            yield ControlConnection(protocol, age, skew)
        finally:
            logger.info("Control relay session ended role=%s session=%s", role, session)
    finally:
        try:
            if protocol is not None:
                await protocol.shutdown()
        finally:
            if handshake is not None:
                if not handshake.done():
                    handshake.cancel()
                await asyncio.gather(handshake, return_exceptions=True)
        if entered:
            await manager.__aexit__(None, None, None)

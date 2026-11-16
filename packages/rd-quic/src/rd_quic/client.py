"""Verified client connection, registration and conservative clock diagnostics."""

import asyncio
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from rd_core.auth import read_secret
from rd_core.configuration import ConfigError
from rd_core.settings import ClientNetworkSettings, ControlSettings, CredentialSettings
from rd_core.wire import ALPN

from .framing import RpcError
from .protocol import RpcProtocol


class ClockError(ConnectionError):
    pass


@dataclass
class ClockGuard:
    max_skew_ms: float
    observed_bound_ms: float = 0
    wall_ms: float = 0
    mono: float = 0

    def check(self):
        if self.mono == 0:
            raise ClockError("clock has not been verified")
        drift = abs(
            time.time_ns() / 1_000_000
            - self.wall_ms
            - (time.monotonic() - self.mono) * 1000
        )
        if drift + self.observed_bound_ms > self.max_skew_ms:
            raise ClockError("local clock changed outside its budget")

    async def probe(self, protocol):
        samples = []
        for _ in range(3):
            start_wall, start_mono = time.time_ns() / 1_000_000, time.monotonic()
            response = await protocol.request("clock", {})
            end_mono, end_wall = time.monotonic(), time.time_ns() / 1_000_000
            server = response.get("server_time_ms")
            if type(server) is not int or server < 0:
                raise ClockError("invalid server clock result")
            # No symmetry assumption: the server observation occurred somewhere
            # between request and response. Include millisecond quantization.
            bound = max(abs(server - start_wall), abs(end_wall - server)) + 1
            if (
                abs((end_wall - start_wall) - (end_mono - start_mono) * 1000)
                > self.max_skew_ms
            ):
                raise ClockError("clock changed during measurement")
            samples.append((bound, end_wall, end_mono))
        best = min(samples)
        if best[0] > self.max_skew_ms:
            raise ClockError("clock or network uncertainty exceeds configured budget")
        self.observed_bound_ms, self.wall_ms, self.mono = best
        self.check()


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
    clock: ClockGuard
    max_control_age_ms: int
    max_clock_skew_ms: int


async def _monitor_clock(protocol, guard):
    try:
        while True:
            await asyncio.sleep(1)
            guard.check()
            await guard.probe(protocol)
    except (ClockError, ConnectionError, TimeoutError, RpcError):
        protocol.close(error_code=0x104, reason_phrase="clock_verification_failed")


@asynccontextmanager
async def registered_connection(
    network: ClientNetworkSettings,
    credentials: CredentialSettings,
    control: ControlSettings,
    *,
    session: int,
    role: str,
    request_handler=None,
    event_handler=None,
    datagram_handler=None,
    on_disconnect=None,
):
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
    monitor = None
    try:
        async with asyncio.timeout(network.register_timeout_s):
            await manager.__aenter__()
            entered = True
            protocol.transmit()
            await protocol.wait_connected()
            result = await protocol.request(
                "register", {"token": token, "session": session, "role": role}
            )
            if result.get("session") != session or result.get("role") != role:
                raise RpcError("registration_mismatch")
            age, skew = (
                result.get("max_control_age_ms"),
                result.get("max_clock_skew_ms"),
            )
            if type(age) is not int or type(skew) is not int or not 0 <= skew < age:
                raise RpcError("invalid_server_limits")
            age = min(age, control.command_timeout_ms)
            if skew > control.max_clock_skew_ms:
                raise RpcError("incompatible_clock_budget")
            # Both clients reserve half the pairwise clock-error budget relative
            # to the same server; never silently compensate capture timestamps.
            guard = ClockGuard(skew / 2)
            await guard.probe(protocol)
        monitor = asyncio.create_task(
            _monitor_clock(protocol, guard), name="rd-clock-monitor"
        )
        yield ControlConnection(protocol, guard, age, skew)
    finally:
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if protocol is not None:
            await protocol.shutdown()
        if entered:
            await manager.__aexit__(None, None, None)

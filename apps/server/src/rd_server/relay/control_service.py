"""Version 2 authenticated relay listener and its owned maintenance task."""

import asyncio
import time

from aioquic.quic.configuration import QuicConfiguration
from rd_core.auth import AuthProvider
from rd_core.settings import RelaySettings
from rd_core.wire import ALPN
from rd_quic.framing import RpcError
from rd_quic.protocol import RpcProtocol

from .registry import Registry
from .service import Listener


class ControlRelayService:
    def __init__(self, settings: RelaySettings, auth: AuthProvider):
        self.settings = settings
        self.registry = Registry(auth, settings)
        self.configuration = QuicConfiguration(
            is_client=False,
            alpn_protocols=[ALPN],
            max_datagram_frame_size=settings.max_datagram_bytes,
            max_stream_data=settings.max_rpc_bytes + 4,
            max_data=(settings.max_rpc_bytes + 4) * 32,
            idle_timeout=settings.idle_timeout_s,
        )
        self.configuration.load_cert_chain(settings.tls.cert, settings.tls.key)
        self.protocols = set()
        self.registration_deadlines = {}
        self.listener = None
        self.maintenance = None

    def _create_protocol(self, *args, **kwargs):
        protocol = RpcProtocol(
            *args,
            on_disconnect=self._disconnect,
            max_rpc_bytes=self.settings.max_rpc_bytes,
            max_datagram_bytes=self.settings.max_datagram_bytes,
            rpc_timeout_s=self.settings.register_timeout_s,
            **kwargs,
        )
        protocol.request_handler = lambda method, params: self._request(
            protocol, method, params
        )
        protocol.event_handler = lambda method, params: self._event(
            protocol, method, params
        )
        protocol.datagram_handler = lambda data: self.registry.route(protocol, data)
        self.protocols.add(protocol)
        self.registration_deadlines[protocol] = (
            time.monotonic() + self.settings.register_timeout_s
        )
        return protocol

    def _disconnect(self, protocol):
        self.registry.unregister(protocol)
        self.registration_deadlines.pop(protocol, None)
        # Keep ownership until listener shutdown, including protocols in the QUIC
        # closing period, so their timers/tasks can always be awaited/cancelled.

    async def _request(self, protocol, method, params):
        if method == "register":
            result = self.registry.register(protocol, params)
            self.registration_deadlines.pop(protocol, None)
            return result
        if method == "clock":
            if params:
                raise RpcError("invalid_parameters")
            return {"server_time_ms": int(time.time() * 1000)}
        self.registry._binding(protocol)
        if method == "acquire" and not params:
            return await self.registry.acquire(protocol)
        if method == "release":
            return self.registry.release(protocol, params)
        raise RpcError("unsupported_method")

    def _event(self, protocol, method, params):
        if method != "release":
            raise RpcError("unsupported_event")
        self.registry.release(protocol, params)

    async def _maintain(self):
        while True:
            self.registry.sweep()
            now = time.monotonic()
            for protocol, deadline in list(self.registration_deadlines.items()):
                if now >= deadline:
                    protocol.close(
                        error_code=0x103, reason_phrase="registration_timeout"
                    )
            for protocol in list(self.protocols):
                if protocol.transport_closed:
                    await protocol.shutdown()
                    self.protocols.discard(protocol)
            await asyncio.sleep(min(self.settings.lease_ttl_s / 4, 0.05))

    async def start(self):
        _, self.listener = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: Listener(
                configuration=self.configuration,
                create_protocol=self._create_protocol,
                retry=True,
            ),
            local_addr=(self.settings.host, self.settings.port),
        )
        self.maintenance = asyncio.create_task(
            self._maintain(), name="relay-maintenance"
        )

    async def stop(self):
        if self.maintenance is not None:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
            self.maintenance = None
        self.registry.close()
        protocols = list(self.protocols)
        try:
            await asyncio.gather(*(protocol.shutdown() for protocol in protocols))
        finally:
            if self.listener is not None:
                self.listener.close()
                await self.listener.closed.wait()
                self.listener = None
            self.protocols.clear()
            self.registration_deadlines.clear()

    async def wait_closed(self):
        # Either a listener or maintenance failure requires a fresh relay lifetime.
        listener_closed = asyncio.create_task(self.listener.closed.wait())
        try:
            done, _ = await asyncio.wait(
                [listener_closed, self.maintenance], return_when=asyncio.FIRST_COMPLETED
            )
            if self.maintenance in done:
                if self.maintenance.cancelled():
                    raise RuntimeError("relay maintenance ended unexpectedly")
                await self.maintenance
        finally:
            listener_closed.cancel()
            await asyncio.gather(listener_closed, return_exceptions=True)

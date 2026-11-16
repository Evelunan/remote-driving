import asyncio

from aioquic.asyncio.server import QuicServer
from aioquic.quic.configuration import QuicConfiguration

from .services.relay_protocol import RelayProtocol
from .services.session_manager import SessionManager


class Listener(QuicServer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = asyncio.Event()

    def connection_lost(self, exc):
        self.closed.set()


class RelayService:
    def __init__(self, host: str, port: int, configuration: QuicConfiguration):
        self.host, self.port, self.configuration = host, port, configuration
        self.manager = SessionManager()
        self.protocols = set()
        self.listener = None

    def _create_protocol(self, *args, **kwargs):
        protocol = RelayProtocol(
            *args, manager=self.manager, on_cleanup=self.protocols.discard, **kwargs
        )
        self.protocols.add(protocol)
        return protocol

    async def start(self):
        _, self.listener = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: Listener(
                configuration=self.configuration,
                create_protocol=self._create_protocol,
                retry=True,
            ),
            local_addr=(self.host, self.port),
        )

    async def stop(self):
        if self.listener is None:
            return
        protocols = list(self.protocols)
        try:
            await asyncio.gather(*(protocol.shutdown() for protocol in protocols))
        finally:
            self.listener.close()
            await self.listener.closed.wait()
            for protocol in protocols:
                protocol.cancel_pending_io()
            self.protocols.clear()
            self.listener = None

    async def wait_closed(self):
        await self.listener.closed.wait()

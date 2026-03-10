import asyncio
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration

from infrastructure.protocol import VehicleProtocol
from config.loaders import QuicConfig
from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)


class VehicleDatagram:

    RETRY_DELAY = 3

    def __init__(self, config: QuicConfig, session_id: str, controller: IController):
        self.host = config.host
        self.port = config.port
        self.session_id = session_id
        self.controller = controller

        self.quic_config = QuicConfiguration(is_client=True)
        self.quic_config.verify_mode = config.options.verify_mode
        self.quic_config.enable_datagrams = config.options.enable_datagrams
        self.quic_config.max_datagram_frame_size = config.options.max_datagram_frame_size
        self.quic_config.idle_timeout = config.options.idle_timeout

    async def start(self):
        logger.info(
            f"[car] Client start (host={self.host}, port={self.port}, session={self.session_id})"
        )
        while True:
            try:
                await self._connect()
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                logger.error(f"[car] Connection failed: {e}")
            except Exception:
                logger.exception("[car] Unexpected client error")
            logger.info(f"[car] reconnect in {self.RETRY_DELAY}s")
            await asyncio.sleep(self.RETRY_DELAY)

    async def _connect(self):
        logger.info("[car] connecting to relay...")
        
        async with connect(
            self.host,
            self.port,
            configuration=self.quic_config,
            create_protocol=self._create_protocol,
        ) as protocol:
            await protocol.wait_connected()
            logger.info(f"[car] QUIC handshake success (session={self.session_id})")
            protocol.start()
            await protocol.wait_closed()
            logger.warning("[car] connection closed")

    def _create_protocol(self, *args, **kwargs):
        return VehicleProtocol(
            self.session_id,
            self.controller,
            *args,
            **kwargs,
        )
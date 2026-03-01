import asyncio
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration

from infrastructure.protocol import DrivingProtocol
from config.loaders import QuicConfig
from logger import get_logger

logger = get_logger(__name__)


class DrivingDatagram:

    def __init__(
        self, config: QuicConfig, session_id: str, packet_queue: asyncio.Queue
    ):
        self.host = config.host
        self.port = config.port
        self.session_id = session_id
        self.packet_queue = packet_queue

        self._business_task: asyncio.Task | None = None

        self.quic_config = QuicConfiguration(is_client=True)
        self.quic_config.verify_mode = config.options.verify_mode
        self.quic_config.enable_datagrams = config.options.enable_datagrams
        self.quic_config.max_datagram_frame_size = (
            config.options.max_datagram_frame_size
        )
        self.quic_config.idle_timeout = config.options.idle_timeout

    async def start(self):
        logger.info(
            f"[driving] Starting client (host={self.host}, port={self.port}, session={self.session_id})"
        )

        while True:
            try:
                logger.info("[driving] Attempting to connect to relay server...")

                async with connect(
                    host=self.host,
                    port=self.port,
                    configuration=self.quic_config,
                    create_protocol=lambda *args, **kwargs: DrivingProtocol(
                        self.session_id,
                        self.packet_queue,
                        *args,
                        **kwargs,
                    ),
                ) as protocol:

                    await protocol.wait_connected()
                    logger.info(
                        f"[driving] QUIC Handshake successful. Session: {self.session_id}"
                    )

                    # 启动业务任务（与车端一致）
                    self._business_task = asyncio.create_task(protocol.start_business())

                    # 等待连接关闭
                    await protocol.wait_closed()

            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                logger.error(f"[driving] Connection failed: {e}. Retrying in 3s...")
                await asyncio.sleep(3)

            except Exception as e:
                logger.exception(f"[driving] Unexpected error: {e}")
                await asyncio.sleep(3)

            finally:
                await self._stop_business_task()

    async def _stop_business_task(self):
        if self._business_task:
            self._business_task.cancel()
            try:
                await self._business_task
            except asyncio.CancelledError:
                pass
            self._business_task = None

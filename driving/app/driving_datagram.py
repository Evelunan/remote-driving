import asyncio
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration

from interfaces import IMessageQueue
from infrastructure.protocol import DrivingProtocol
from logger import get_logger

logger = get_logger(__name__)


class DrivingDatagram:

    RETRY_DELAY = 3

    def __init__(
        self,
        host,
        port,
        quic_config: QuicConfiguration,
        session_id: str,
        processed_queue: IMessageQueue,
    ):

        self.host = host
        self.port = port
        self.quic_config = quic_config
        self.session_id = session_id
        self.processed_queue = processed_queue

    # =========================
    # 启动客户端
    # =========================
    async def start(self):
        while True:
            try:
                await self._connect()
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                logger.error(f"连接失败: {e}")
            except Exception:
                logger.exception("QUIC客户端异常")
            logger.info(f"{self.RETRY_DELAY}s 后重试连接...")
            await asyncio.sleep(self.RETRY_DELAY)

    # =========================
    # 单次连接
    # =========================
    async def _connect(self):
        logger.info(f"连接到 {self.host}:{self.port}")
        async with connect(
            self.host,
            self.port,
            configuration=self.quic_config,
            create_protocol=self._create_protocol,
        ) as protocol:
            try:
                await protocol.wait_connected()
                logger.info("QUIC握手完成")
                protocol.start()
                await protocol.wait_closed()
            finally:
                await protocol.shutdown()

    # =========================
    # 创建协议
    # =========================
    def _create_protocol(self, *args, **kwargs):
        return DrivingProtocol(
            self.session_id,
            self.processed_queue,
            *args,
            **kwargs,
        )

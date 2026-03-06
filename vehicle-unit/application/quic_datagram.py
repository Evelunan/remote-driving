import asyncio
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration

from infrastructure.protocol import CarProtocol
from config.loaders import QuicConfig
from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)


class QuicDatagram:

    def __init__(
        self, config: QuicConfig, session_id: str, controller: IController = None
    ):
        self.host = config.host
        self.port = config.port
        self.session_id = session_id
        self.controller = controller

        self._business_task: asyncio.Task | None = None

        self.quic_config = QuicConfiguration(is_client=True)
        self.quic_config.verify_mode = config.options.verify_mode
        self.quic_config.enable_datagrams = config.options.enable_datagrams
        self.quic_config.max_datagram_frame_size = (
            config.options.max_datagram_frame_size
        )
        self.quic_config.idle_timeout = config.options.idle_timeout

    async def start(self):
        """启动客户端（带重连机制）"""
        logger.info(
            f"Starting client (host={self.host}, port={self.port}, session={self.session_id})"
        )

        while True:
            try:
                logger.info(f"Attempting to connect to relay server...")
                async with connect(
                    self.host,
                    self.port,
                    configuration=self.quic_config,
                    create_protocol=lambda *args, **kwargs: CarProtocol(
                        self.session_id, self.controller, *args, **kwargs
                    ),
                ) as protocol:
                    # 关键：确保 QUIC 握手完成后再继续
                    await protocol.wait_connected()
                    logger.info(
                        f"[car] QUIC Handshake successful. Session: {self.session_id}"
                    )

                    # 启动业务任务（Hello + Heartbeat）
                    protocol._business_task = asyncio.create_task(
                        protocol.start_business()
                    )

                    # 挂起协程，直到连接关闭或发生异常
                    await protocol.wait_closed()

            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                logger.error(f"[car] Connection failed: {e}. Retrying in 3s...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"[car] Unexpected error: {e}. Restarting...")
                await asyncio.sleep(3)

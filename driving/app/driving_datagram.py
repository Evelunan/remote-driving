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
        while True:
            try:
                logger.info(f"尝试连接到 {self.host}:{self.port}...")

                async with connect(
                    self.host,
                    self.port,
                    configuration=self.quic_config,
                    create_protocol=lambda *args, **kw: DrivingProtocol(
                        self.session_id, self.packet_queue, *args, **kw
                    ),
                ) as protocol:
                    # 等待握手完成
                    await protocol.wait_connected()
                    logger.info("成功与中继服务器建立连接并完成握手")

                    # 启动业务任务
                    protocol._business_task = asyncio.create_task(
                        protocol.start_business()
                    )

                    # 保持连接直到断开
                    await protocol.wait_closed()

            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                logger.error(f"连接失败: {e}. 3秒后重试...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.exception(f"发生意外错误: {e}. 3秒后重启...")
                await asyncio.sleep(3)

import asyncio
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration

from infrastructure.protocol import DrivingProtocol
from infrastructure.communication import ControlState
from config.loaders import QuicConfig
from logger import get_logger

logger = get_logger(__name__)


class DrivingDatagram:

    RETRY_DELAY = 3

    def __init__(
        self, config: QuicConfig, session_id: str, control_state: ControlState
    ):

        self.host = config.host
        self.port = config.port
        self.session_id = session_id
        self.control_state = control_state

        self.quic_config = QuicConfiguration(is_client=True)
        self.quic_config.verify_mode = config.options.verify_mode
        self.quic_config.enable_datagrams = config.options.enable_datagrams
        self.quic_config.max_datagram_frame_size = (
            config.options.max_datagram_frame_size
        )
        self.quic_config.idle_timeout = config.options.idle_timeout

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

            await protocol.wait_connected()

            logger.info("QUIC握手完成")

            protocol.start()

            await protocol.wait_closed()

            logger.warning("连接关闭")

    # =========================
    # 创建协议
    # =========================
    def _create_protocol(self, *args, **kwargs):

        return DrivingProtocol(
            self.session_id,
            self.control_state,
            *args,
            **kwargs,
        )

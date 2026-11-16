import asyncio
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from rd_core.control import ControlSupervisor

from infrastructure.protocol import VehicleProtocol
from config.loaders import QuicConfig
from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)


class VehicleDatagram:

    RETRY_DELAY = 3

    def __init__(self, config: QuicConfig, session_id: str, controller: IController, *, control_timeout_s=0.3):
        self.host = config.host
        self.port = config.port
        self.session_id = session_id
        self.controller = controller
        self.control_timeout_s = control_timeout_s
        self.supervisor = None
        self._protocol = None
        self._loop = None

        self.quic_config = QuicConfiguration(is_client=True)
        self.quic_config.verify_mode = config.options.verify_mode
        self.quic_config.enable_datagrams = config.options.enable_datagrams
        self.quic_config.max_datagram_frame_size = config.options.max_datagram_frame_size
        self.quic_config.idle_timeout = config.options.idle_timeout

    async def start(self):
        logger.info(
            f"[car] Client start (host={self.host}, port={self.port}, session={self.session_id})"
        )
        self._loop = asyncio.get_running_loop()
        self.supervisor = ControlSupervisor(
            self.controller, timeout_s=self.control_timeout_s, sequence_bits=16,
            on_result=self._post_result,
        )
        try:
            while True:
                try:
                    await self._connect()
                except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                    logger.error("[car] Connection failed: %s", e)
                except Exception:
                    logger.exception("[car] Unexpected client error")
                await asyncio.sleep(self.RETRY_DELAY)
        finally:
            self.supervisor.stop("control client stopped")
            if not await asyncio.to_thread(self.supervisor.close):
                logger.error("Device worker did not exit; independent chassis safety action required")

    def _post_result(self, command, executed, detail):
        self._loop.call_soon_threadsafe(self._deliver_result, command, executed, detail)

    def _deliver_result(self, command, executed, detail):
        if self._protocol is not None:
            self._protocol.execution_result(command, executed, detail)

    async def _connect(self):
        logger.info("[car] connecting to relay...")
        
        async with connect(
            self.host,
            self.port,
            configuration=self.quic_config,
            create_protocol=self._create_protocol,
        ) as protocol:
            self._protocol = protocol
            try:
                await protocol.wait_connected()
                protocol.start()
                await protocol.wait_closed()
            finally:
                await protocol.shutdown()
                self._protocol = None

    def _create_protocol(self, *args, **kwargs):
        return VehicleProtocol(
            self.session_id,
            self.controller,
            *args,
            supervisor=self.supervisor,
            **kwargs,
        )

import asyncio
import json
from typing import TYPE_CHECKING
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

if TYPE_CHECKING:
    from communication import ControlState
from logger import get_logger

logger = get_logger(__name__)


class DrivingProtocol(QuicConnectionProtocol):

    def __init__(
        self,
        session_id,
        control_state: "ControlState",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.session_id = session_id
        self.control_state = control_state

        self._connected = False
        self._business_task: asyncio.Task | None = None

    # =========================
    # 连接建立
    # =========================
    def connection_made(self, transport):
        super().connection_made(transport)

        self._connected = True
        logger.info("QUIC 传输已建立，等待握手完成...")

    # =========================
    # QUIC事件
    # =========================
    def quic_event_received(self, event):

        if isinstance(event, ConnectionTerminated):
            self._handle_connection_closed(event)

        elif isinstance(event, DatagramFrameReceived):
            self._handle_datagram(event.data)

    # =========================
    # 连接关闭处理
    # =========================
    def _handle_connection_closed(self, event):

        logger.warning(
            f"连接终止: code={event.error_code}, reason={event.reason_phrase}"
        )

        self._connected = False

        if self._business_task and not self._business_task.done():
            self._business_task.cancel()

    # =========================
    # 收到服务器数据
    # =========================
    def _handle_datagram(self, data: bytes):

        try:
            message = json.loads(data.decode())
            logger.debug(f"收到服务器消息: {message}")

        except json.JSONDecodeError:
            logger.warning(f"收到非法JSON: {data!r}")

    # =========================
    # 启动业务
    # =========================
    def start(self):

        if not self._business_task:
            self._business_task = asyncio.create_task(self._business_loop())

    # =========================
    # 业务主循环
    # =========================
    async def _business_loop(self):

        try:
            await self._send_hello()

            logger.info("控制循环启动")
            last_ts = 0
            while self._connected:
                state = self.control_state
                # 如果状态更新才发送
                if state.timestamp != last_ts:
                    last_ts = state.timestamp
                    await self._send_control(state.linear, state.angular)
                await asyncio.sleep(0.02)  # 50Hz

        except asyncio.CancelledError:
            logger.info("业务循环被取消")

        except Exception:
            logger.exception("业务循环异常")

    # =========================
    # 发送JSON
    # =========================
    def _send_json(self, data: dict):

        try:
            payload = json.dumps(data).encode()

            self._quic.send_datagram_frame(payload)
            self.transmit()

        except Exception:
            logger.exception("发送数据失败")

    # =========================
    # hello
    # =========================
    async def _send_hello(self):

        msg = {
            "session_id": self.session_id,
            "type": "hello-driving",
        }

        self._send_json(msg)

        logger.info(f"身份发送完成 session={self.session_id}")

    # =========================
    # control
    # =========================
    async def _send_control(self, linear, angular):

        msg = {
            "session_id": self.session_id,
            "type": "control",
            "data": {"linear": linear, "angular": angular},
        }
        self._send_json(msg)
        logger.info(f"发送控制: v={linear}, w={angular}")

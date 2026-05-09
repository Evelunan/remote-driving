import asyncio
import time
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from common.constants import ROOT_PATH
from interfaces.icontroller import IController
from logger import get_logger

# from .control_codec import ControlCodec
from .codec import ControlCodec

logger = get_logger(__name__)


class VehicleProtocol(QuicConnectionProtocol):

    def __init__(self, session_id, controller: IController, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.controller = controller
        self._connected = False
        self._business_task: asyncio.Task | None = None

        self._last_seq = -1
        self._last_timestamp = -1

        self._batch_start_seq = -1   # 当前批次第一个包的 seq
        self._received_count = 0       # 当前批次已收到包数

    # =========================
    # connection lifecycle
    # =========================

    def connection_made(self, transport):
        super().connection_made(transport)
        self._connected = True
        logger.info("[car] UDP transport established, waiting for QUIC handshake...")

    def connection_lost(self, exc):
        logger.warning(f"[car] Connection lost: {exc}")
        self._connected = False
        self._stop_business()
        super().connection_lost(exc)

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            logger.warning(
                f"[car] Connection terminated: {event.error_code} {event.reason_phrase}"
            )
            self._connected = False
            self._stop_business()
        elif isinstance(event, DatagramFrameReceived):
            self._handle_datagram(event.data)

    # =========================
    # business lifecycle
    # =========================

    def start(self):
        """启动业务循环"""
        if not self._business_task:
            self._business_task = asyncio.create_task(self._business_loop())

    def _stop_business(self):
        if self._business_task and not self._business_task.done():
            self._business_task.cancel()

    async def _business_loop(self):
        try:
            await self._send_hello()
            await self._heartbeat_loop()
        except asyncio.CancelledError:
            logger.info("[car] Business loop cancelled")
        except Exception:
            logger.exception("[car] Business loop error")

    # =========================
    # datagram
    # =========================

    def _send_datagram(self, payload: bytes):
        try:
            self._quic.send_datagram_frame(payload)
            self.transmit()
        except Exception:
            logger.exception("[car] Failed to send datagram")
            self._connected = False

    def _handle_datagram(self, data: bytes):
        try:
            msg = ControlCodec.decode(data)
            msg_type = msg.get("type")
            if msg_type == ControlCodec.MessageType.CONTROL:
                self.process_control_message(msg)
            elif msg_type == ControlCodec.MessageType.HEARTBEAT:
                logger.debug(msg)
            else:
                logger.debug(f"[car] Ignore message type: {msg_type}")
        except Exception:
            logger.exception("[car] Failed to parse datagram")

    # =========================
    # hello
    # =========================

    async def _send_hello(self):
        msg = ControlCodec.encode(ControlCodec.MessageType.HELLO_CAR, self.session_id)
        self._send_datagram(msg)
        logger.info(f"[car] hello sent (session={self.session_id})")

    # =========================
    # heartbeat
    # =========================

    async def _heartbeat_loop(self):
        logger.info("[car] Heartbeat loop started")
        while self._connected:
            heartbeat = ControlCodec.encode(
                ControlCodec.MessageType.HEARTBEAT, self.session_id
            )
            self._send_datagram(heartbeat)
            await asyncio.sleep(3)
        logger.info("[car] Heartbeat loop stopped")

    # =========================
    # control message
    # =========================

    def process_control_message(self, message: dict):
        if message is None:
            return
        # 接收端
        seq = message["seq"]

        # 测试使用
        # # 批次起始：记录第一个包的 seq
        # if self._batch_start_seq == -1:
        #     self._batch_start_seq = seq

        # self._received_count += 1

        # if self._received_count == 100:
        #     # 这批次发送端理论上发了多少个包（seq 跨度）
        #     expected = (seq - self._batch_start_seq + 1) & 0xFFFF
        #     loss_rate = max(0.0, (expected - 100) / expected) if expected > 0 else 0.0

        #     logger.info(f"[car] 批次丢包率 - 期望: {expected}, 收到: 100, 丢包率: {loss_rate:.2%}")

        #     try:
        #         log_file = ROOT_PATH / "logs" / "packet_loss_stats.csv"
        #         with open(log_file, "a") as f:
        #             f.write(f"{time.time()},{expected},100,{loss_rate:.4f}\n")
        #     except Exception as e:
        #         logger.error(f"记录丢包率失败: {e}")

        #     # 重置批次
        #     self._batch_start_seq = -1
        #     self._received_count = 0
            
        if not ControlCodec.is_newer(seq, self._last_seq):
            return
        self._last_seq = seq

        throttle, brake, steering = (
            message["throttle"],
            message["brake"],
            message["steering"],
        )

        # 只修改类型，原封不动的发回去，进行时延测试
        data = ControlCodec.encode(
            type=ControlCodec.MessageType.STATE,
            session=self.session_id,
            throttle=throttle,
            brake=brake,
            steering=steering,
            timestamp=message["timestamp"],
            seq=seq,
        )
        logger.info(message)
        self._send_datagram(data)

        if self.controller == None:
            return
        try:
            self.controller.send_command(throttle, brake, steering)
        except Exception:
            logger.exception("[car] control execution failed")

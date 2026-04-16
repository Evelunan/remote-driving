import asyncio
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

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
        if not ControlCodec.is_newer(seq, self._last_seq):
            return
        self._last_seq = seq

        throttle, brake, steering = (
            message["throttle"],
            message["brake"],
            message["steering"],
        )
        
        # 发给驾驶端，用于测试时延
        data = ControlCodec.encode(
            type=ControlCodec.MessageType.STATE,
            session=self.session_id,
            throttle=throttle,
            brake=brake,
            steering=steering,
            timestamp=message["timestamp"],
            seq=seq,
        )
        self._send_datagram(data)

        if self.controller == None:
            return
        try:
            self.controller.send_command(throttle, brake, steering)
        except Exception:
            logger.exception("[car] control execution failed")

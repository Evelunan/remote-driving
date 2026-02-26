import asyncio
import json
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)


class CarProtocol(QuicConnectionProtocol):

    def __init__(self, session_id, controller: IController, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.controller = controller

    # =========================================================
    # Connection Lifecycle
    # =========================================================

    def connection_made(self, transport):
        super().connection_made(transport)
        logger.info("[car] UDP transport established, performing QUIC handshake...")

    def connection_lost(self, exc):
        logger.warning(f"[car] Connection lost: {exc}")
        super().connection_lost(exc)

    def quic_event_received(self, event):

        if isinstance(event, ConnectionTerminated):
            logger.warning(
                f"[car] Connection terminated: {event.error_code} - {event.reason_phrase}"
            )

        elif isinstance(event, DatagramFrameReceived):
            self._handle_datagram(event.data)

    # =========================================================
    # Business
    # =========================================================

    async def start_business(self):
        try:
            await self._send_hello()
            await self._heartbeat_loop()
        except asyncio.CancelledError:
            logger.info("[car] Business task cancelled")
            raise


    # =========================================================
    # Hello & Heartbeat
    # =========================================================

    async def _send_hello(self):
        await asyncio.sleep(0.5)

        self._send_json({"session_id": self.session_id, "type": "hello-car"})

        logger.info(f"[car] Identity sent (Session: {self.session_id})")

    async def _heartbeat_loop(self):
        logger.info("[car] Starting heartbeat loop...")

        while True:
            self._send_json(
                {
                    "session_id": self.session_id,
                    "role": "car",
                    "type": "heartbeat",
                    "timestamp": int(asyncio.get_running_loop().time()),
                }
            )

            await asyncio.sleep(20)  # 如果需要，也可以由外部注入

    # =========================================================
    # Datagram Handling
    # =========================================================

    def _handle_datagram(self, data: bytes):
        try:
            msg = json.loads(data.decode())
        except json.JSONDecodeError:
            logger.error("[car] Invalid JSON received")
            return

        self._dispatch_message(msg)

    def _dispatch_message(self, msg: dict):
        msg_type = msg.get("type")

        if msg_type == "control":
            self._process_control(msg)

        elif msg_type == "ping":
            logger.debug("[car] Ping received")

        else:
            logger.warning(f"[car] Unknown message type: {msg_type}")

    # =========================================================
    # Control Processing
    # =========================================================

    def _process_control(self, message: dict):
        data = self._parse_data_field(message.get("data"))
        if not data:
            return

        try:
            linear = float(data.get("linear", 0.0))
            angular = float(data.get("angular", 0.0))
        except (TypeError, ValueError):
            logger.error("[car] Invalid control values")
            return

        logger.info(f"[car] Forward control: linear={linear}, angular={angular}")

        self.controller.send_command(linear, angular)

    # =========================================================
    # Utilities
    # =========================================================

    def _parse_data_field(self, data_field):
        if isinstance(data_field, dict):
            return data_field

        if isinstance(data_field, str):
            try:
                return json.loads(data_field)
            except json.JSONDecodeError:
                logger.error("[car] Invalid control JSON string")
                return None

        logger.warning(f"[car] Unexpected data type: {type(data_field)}")
        return None

    def _send_json(self, payload: dict):
        try:
            data = json.dumps(payload).encode()
            self._quic.send_datagram_frame(data)
            self.transmit()
        except Exception as e:
            logger.error(f"[car] Failed to send datagram: {e}")

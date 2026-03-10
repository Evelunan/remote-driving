import asyncio
import json
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)


class VehicleProtocol(QuicConnectionProtocol):

    def __init__(self, session_id, controller: IController, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.controller = controller
        self._connected = False
        self._business_task: asyncio.Task | None = None

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

    def _send_json(self, data: dict):
        if not self._connected:
            return
        try:
            payload = json.dumps(data).encode()
            self._quic.send_datagram_frame(payload)
            self.transmit()
        except Exception:
            logger.exception("[car] Failed to send datagram")
            self._connected = False

    def _handle_datagram(self, data: bytes):
        try:
            msg = json.loads(data.decode())
            msg_type = msg.get("type")
            if msg_type == "control":
                self.process_control_message(msg)
            elif msg_type == "ping":
                pass
            else:
                logger.debug(f"[car] Ignore message type: {msg_type}")
        except Exception:
            logger.exception("[car] Failed to parse datagram")

    # =========================
    # hello
    # =========================

    async def _send_hello(self):
        msg = {
            "session_id": self.session_id,
            "type": "hello-car",
        }
        self._send_json(msg)
        logger.info(f"[car] hello sent (session={self.session_id})")

    # =========================
    # heartbeat
    # =========================

    async def _heartbeat_loop(self):
        logger.info("[car] Heartbeat loop started")
        while self._connected:
            heartbeat = {
                "session_id": self.session_id,
                "role": "car",
                "type": "heartbeat",
                "timestamp": int(asyncio.get_running_loop().time()),
            }

            self._send_json(heartbeat)
            await asyncio.sleep(3)
        logger.info("[car] Heartbeat loop stopped")

    # =========================
    # control message
    # =========================

    def process_control_message(self, message: dict):
        data = self._parse_data_field(message.get("data"))
        if data is None:
            return
        linear, angular = self._extract_control_values(data)
        logger.info(f"[car] control: linear={linear}, angular={angular}")
        try:
            self.controller.send_command(linear, angular)
        except Exception:
            logger.exception("[car] control execution failed")

    def _parse_data_field(self, data_field):
        if isinstance(data_field, str):
            return self._parse_json_string(data_field)
        if isinstance(data_field, dict):
            return data_field
        logger.warning(f"[car] unexpected data type: {type(data_field)}")
        return None

    def _extract_control_values(self, data: dict):
        linear = float(data.get("linear", 0.0))
        angular = float(data.get("angular", 0.0))
        return linear, angular

    def _parse_json_string(self, json_str):
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logger.error(f"[car] invalid json: {json_str}")
            return None
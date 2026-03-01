import asyncio
import json
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from logger import get_logger

logger = get_logger(__name__)


class DrivingProtocol(QuicConnectionProtocol):

    def __init__(self, session_id, packet_queue: asyncio.Queue, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.packet_queue = packet_queue

    # =========================================================
    # Connection Lifecycle
    # =========================================================

    def connection_made(self, transport):
        super().connection_made(transport)
        logger.info("[driving] UDP transport established, performing QUIC handshake...")

    def connection_lost(self, exc):
        logger.warning(f"[driving] Connection lost: {exc}")
        super().connection_lost(exc)

    def quic_event_received(self, event):

        if isinstance(event, ConnectionTerminated):
            logger.warning(
                f"[driving] Connection terminated: {event.error_code} - {event.reason_phrase}"
            )

        elif isinstance(event, DatagramFrameReceived):
            self._handle_datagram(event.data)

    # =========================================================
    # Business
    # =========================================================

    async def start_business(self):
        try:
            await self._send_hello()
            await self._control_loop()
        except asyncio.CancelledError:
            logger.info("[driving] Business task cancelled")
            raise

    # =========================================================
    # Hello + Control Loop
    # =========================================================

    async def _send_hello(self):
        await asyncio.sleep(0.5)

        self._send_json(
            {
                "session_id": self.session_id,
                "type": "hello-driving",
            }
        )

        logger.info(f"[driving] Identity sent (Session: {self.session_id})")

    async def _control_loop(self):
        logger.info("[driving] Starting control loop...")

        while True:
            ctrl_data = await self.packet_queue.get()

            self._send_json(
                {
                    "session_id": self.session_id,
                    "type": "control",
                    "data": ctrl_data,
                }
            )

            logger.debug(f"[driving] Control sent: {ctrl_data}")

    # =========================================================
    # Datagram Handling
    # =========================================================

    def _handle_datagram(self, data: bytes):
        try:
            msg = json.loads(data.decode())
        except json.JSONDecodeError:
            logger.error("[driving] Invalid JSON received")
            return

        logger.info(f"[driving] Message received: {msg}")

    # =========================================================
    # Utilities
    # =========================================================

    def _send_json(self, payload: dict):
        try:
            data = json.dumps(payload).encode()
            self._quic.send_datagram_frame(data)
            self.transmit()
        except Exception as e:
            logger.error(f"[driving] Failed to send datagram: {e}")
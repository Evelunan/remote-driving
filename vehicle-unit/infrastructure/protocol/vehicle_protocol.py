"""Legacy wire adapter; device I/O and expiry belong to ControlSupervisor."""

import asyncio
import time
import uuid

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import ConnectionTerminated, DatagramFrameReceived, HandshakeCompleted
from rd_core.control import Command, ControlSupervisor

from logger import get_logger
from .codec import ControlCodec

logger = get_logger(__name__)


class VehicleProtocol(QuicConnectionProtocol):
    def __init__(self, session_id, controller, *args, supervisor: ControlSupervisor | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.controller = controller
        self.supervisor = supervisor
        self.epoch = uuid.uuid4().hex
        self._business_connected = False
        self._business_task: asyncio.Task | None = None

    def connection_made(self, transport):
        super().connection_made(transport)
        logger.info("[car] UDP transport established; awaiting TLS")

    def connection_lost(self, exc):
        self._stop_business("QUIC transport lost")
        super().connection_lost(exc)

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self._stop_business("QUIC connection terminated")
        elif isinstance(event, HandshakeCompleted):
            self._business_connected = True
        elif isinstance(event, DatagramFrameReceived):
            self._handle_datagram(event.data)
        super().quic_event_received(event)

    def start(self):
        if not self._business_connected:
            raise RuntimeError("QUIC handshake is not complete")
        if self._business_task is None or self._business_task.done():
            if self.supervisor is None:
                raise RuntimeError("control supervisor is required")
            self.supervisor.begin_session(self.epoch)
            self._business_task = asyncio.create_task(self._business_loop())

    def _stop_business(self, reason="control session stopped"):
        self._business_connected = False
        if self._business_task:
            self._business_task.cancel()
        if self.supervisor is not None:
            self.supervisor.stop(reason, epoch=self.epoch)

    async def shutdown(self):
        self._stop_business()
        if self._business_task:
            await asyncio.gather(self._business_task, return_exceptions=True)
            self._business_task = None

    async def _business_loop(self):
        try:
            self._send_datagram(ControlCodec.encode(ControlCodec.MessageType.HELLO_CAR, self.session_id))
            while self._business_connected:
                self._send_datagram(ControlCodec.encode(ControlCodec.MessageType.HEARTBEAT, self.session_id))
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stop_business("QUIC sender failed")
            logger.exception("[car] sender failed")

    def _send_datagram(self, payload: bytes):
        self._quic.send_datagram_frame(payload)
        self.transmit()

    def _handle_datagram(self, data: bytes):
        try:
            message = ControlCodec.decode(data)
        except (ValueError, KeyError):
            return
        if message["session"] != self.session_id or not self._business_connected:
            return
        if message["type"] == ControlCodec.MessageType.CONTROL:
            self.process_control_message(message)

    def process_control_message(self, message: dict):
        if self.supervisor is None or not self._business_connected:
            return
        if message["session"] != self.session_id:
            return
        # Legacy wall-clock timestamp wraps every 2**32 ms. Require synchronized
        # clocks and reject future/expired packets; v2 will carry explicit age.
        now_ms = (time.time_ns() // 1_000_000) & 0xFFFFFFFF
        age_s = ((now_ms - message["timestamp"]) & 0xFFFFFFFF) / 1000
        self.supervisor.submit(Command(
            self.epoch, message["seq"], message["throttle"], message["brake"],
            message["steering"], message["timestamp"],
        ), age_s=age_s)

    def execution_result(self, command: Command, executed: bool, detail: str):
        if command.epoch != self.epoch or not self._business_connected:
            return
        if not executed:
            logger.debug("[car] control not executed: %s", detail)
            return
        # Transitional STATE is an execution acknowledgment, never odometry.
        self._send_datagram(ControlCodec.encode(
            ControlCodec.MessageType.STATE, self.session_id,
            throttle=command.throttle, brake=command.brake, steering=command.steering,
            timestamp=command.source_timestamp, seq=command.seq,
        ))

import asyncio
import inspect
import math
import time

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import ConnectionTerminated, DatagramFrameReceived, HandshakeCompleted
from rd_core.samples import ControlInput

from logger import get_logger
from .codec import ControlCodec

logger = get_logger(__name__)


class DrivingProtocol(QuicConnectionProtocol):
    def __init__(self, session_id, processed_queue, *args, input_timeout_s=0.2, send_hz=50.0, **kwargs):
        super().__init__(*args, **kwargs)
        if not all(math.isfinite(v) and v > 0 for v in (input_timeout_s, send_hz)):
            raise ValueError("invalid control timing")
        if input_timeout_s <= 1 / send_hz:
            raise ValueError("input timeout must exceed a send period")
        self.session_id = int(session_id)
        self.processed_queue = processed_queue
        self.input_timeout_s = input_timeout_s
        self.period_s = 1 / send_hz
        self._business_connected = False
        self._business_task = None
        self._state_task = None
        self._state_callback = None
        self._latest_state = None

    def connection_made(self, transport):
        super().connection_made(transport)
        logger.info("QUIC transport established; awaiting TLS")

    def connection_lost(self, exc):
        self._stop_tasks()
        super().connection_lost(exc)

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self._stop_tasks()
        elif isinstance(event, DatagramFrameReceived):
            self._handle_datagram(event.data)
        elif isinstance(event, HandshakeCompleted):
            self._business_connected = True
        super().quic_event_received(event)

    def _stop_tasks(self):
        self._business_connected = False
        for task in (self._business_task, self._state_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    async def shutdown(self):
        if self._business_connected:
            try:
                self._send_datagram(ControlCodec.encode(ControlCodec.MessageType.STOP, self.session_id))
            except Exception:
                logger.debug("STOP delivery failed during shutdown", exc_info=True)
        self._stop_tasks()
        tasks = [task for task in (self._business_task, self._state_task) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)

    def _handle_datagram(self, data):
        try:
            message = ControlCodec.decode(data)
        except (ValueError, KeyError):
            return
        if not self._business_connected or message["session"] != self.session_id:
            return
        if message["type"] == ControlCodec.MessageType.STATE and self._state_callback:
            self._latest_state = message
            if self._state_task is None or self._state_task.done():
                self._state_task = asyncio.create_task(self._deliver_states())

    async def _deliver_states(self):
        while self._latest_state is not None and self._business_connected:
            message, self._latest_state = self._latest_state, None
            result = self._state_callback(message)
            if inspect.isawaitable(result):
                await result

    def start(self):
        if not self._business_connected:
            raise RuntimeError("QUIC handshake is not complete")
        if self._business_task is None or self._business_task.done():
            self._business_task = asyncio.create_task(self._business_loop())

    async def _business_loop(self):
        sample = None
        next_tick = time.monotonic()
        try:
            self._send_datagram(ControlCodec.encode(ControlCodec.MessageType.HELLO_DRIVING, self.session_id))
            while self._business_connected:
                now = time.monotonic()
                if now >= next_tick:
                    if sample is not None:
                        if not isinstance(sample, ControlInput) or not sample.fresh(self.input_timeout_s, now=now):
                            self._invalidate_input()
                            return
                        await self._send_control(sample)
                    # Skip missed periods; never burst to catch up after a stall.
                    next_tick += (math.floor((now - next_tick) / self.period_s) + 1) * self.period_s
                try:
                    sample = await asyncio.wait_for(self.processed_queue.get(), max(0.0, next_tick - time.monotonic()))
                    if not isinstance(sample, ControlInput) or not sample.valid:
                        self._invalidate_input()
                        return
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("control sender failed")
            self.close(error_code=0x101, reason_phrase="control sender failed")
        finally:
            self._business_connected = False

    def _invalidate_input(self):
        self._send_datagram(ControlCodec.encode(ControlCodec.MessageType.STOP, self.session_id))
        self._business_connected = False
        self.close(error_code=0x100, reason_phrase="input unavailable or expired")

    def _send_datagram(self, payload):
        self._quic.send_datagram_frame(payload)
        self.transmit()

    async def _send_control(self, sample: ControlInput):
        self._send_datagram(ControlCodec.encode(
            ControlCodec.MessageType.CONTROL, self.session_id,
            throttle=sample.throttle, brake=sample.brake, steering=sample.steering,
            timestamp=sample.timestamp_ms & 0xFFFFFFFF,
        ))

    def set_state_callback(self, callback):
        self._state_callback = callback

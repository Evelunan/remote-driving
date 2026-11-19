"""Reliable bounded RPC, owned handler tasks, and latest-only datagrams."""

import asyncio

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import (
    ConnectionTerminated,
    DatagramFrameReceived,
    HandshakeCompleted,
    StopSendingReceived,
    StreamDataReceived,
    StreamReset,
)
from rd_core.async_compat import async_timeout
from rd_core.logging import get_logger
from rd_core.wire import ALPN

from .compat import (
    cancel_pending_io,
    finish_abandoned_close,
    has_stream_capacity,
    send_latest_datagram,
)
from .framing import MessageBuffer, RpcError, encode_message

logger = get_logger(__name__)


class RpcProtocol(QuicConnectionProtocol):
    def __init__(
        self,
        *args,
        request_handler=None,
        event_handler=None,
        datagram_handler=None,
        on_disconnect=None,
        max_rpc_bytes=16384,
        max_datagram_bytes=1200,
        max_streams=32,
        rpc_timeout_s=5.0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if not (
            0 < max_rpc_bytes <= 1_048_576
            and 0 < max_datagram_bytes <= 65536
            and 0 < max_streams <= 256
            and 0 < rpc_timeout_s <= 60
        ):
            raise ValueError("invalid transport bounds")
        self.request_handler = request_handler
        self.event_handler = event_handler
        self.datagram_handler = datagram_handler
        self.on_disconnect = on_disconnect
        self.max_rpc_bytes = max_rpc_bytes
        self.max_datagram_bytes = max_datagram_bytes
        self.max_streams = max_streams
        self.rpc_timeout_s = rpc_timeout_s
        self.rpc_ready = False
        self._rpc_closed = False
        self._pending = {}
        self._buffers = {}
        self._tasks = {}
        self._receive_deadlines = {}

    @property
    def transport_closed(self):
        return self._closed.is_set()

    def _ensure_ready(self):
        if self._rpc_closed or not self.rpc_ready:
            raise ConnectionError("QUIC handshake is not ready")

    def _send(self, stream_id, message):
        data = encode_message(message, self.max_rpc_bytes)
        self._quic.send_stream_data(stream_id, data, end_stream=True)
        self.transmit()

    def _new_stream(self, *, unidirectional=False):
        self._ensure_ready()
        if not has_stream_capacity(self, self.max_streams * 4):
            raise RpcError("stream_backpressure")
        return self._quic.get_next_available_stream_id(is_unidirectional=unidirectional)

    async def request(self, method, params, *, timeout_s=None):
        self._ensure_ready()
        if len(self._pending) >= self.max_streams:
            raise RpcError("too_many_requests")
        stream_id = self._new_stream()
        future = self._loop.create_future()
        self._pending[stream_id] = future
        sent = False
        try:
            self._send(
                stream_id, {"type": "request", "method": method, "params": params}
            )
            sent = True
            deadline_s = self.rpc_timeout_s if timeout_s is None else timeout_s
            async with async_timeout(deadline_s):
                return await future
        finally:
            self._pending.pop(stream_id, None)
            self._discard_buffer(stream_id)
            if not future.done():
                future.cancel()
            if sent and future.cancelled() and not self._rpc_closed:
                self._quic.stop_stream(stream_id, 0x101)
                self.transmit()

    def notify(self, method, params):
        self._ensure_ready()
        # Notifications have no application reply and use one-way streams.
        stream_id = self._new_stream(unidirectional=True)
        self._send(stream_id, {"type": "event", "method": method, "params": params})

    def send_datagram(self, data):
        self._ensure_ready()
        if not 0 < len(data) <= self.max_datagram_bytes:
            raise ValueError("datagram exceeds configured limit")
        # Discard an unsent stale datagram when congestion prevents transmission.
        send_latest_datagram(self, data)

    def _discard_buffer(self, stream_id):
        self._buffers.pop(stream_id, None)
        deadline = self._receive_deadlines.pop(stream_id, None)
        if deadline is not None:
            deadline.cancel()

    def _receive_timeout(self, stream_id):
        self._discard_buffer(stream_id)
        self.close(error_code=0x102, reason_phrase="stream_timeout")

    def _stream_data(self, event):
        stream_id = event.stream_id
        local = (stream_id & 1) == (0 if self._quic.configuration.is_client else 1)
        if local and stream_id not in self._pending:
            # Late replies cannot complete a newer request; QUIC stream ids are
            # never reused. Close receive state without allocating a buffer.
            self._quic.stop_stream(stream_id, 0x101)
            self.transmit()
            return
        if stream_id not in self._buffers:
            if len(self._buffers) + len(self._tasks) >= self.max_streams:
                raise RpcError("too_many_streams")
            self._buffers[stream_id] = MessageBuffer(self.max_rpc_bytes)
            self._receive_deadlines[stream_id] = self._loop.call_later(
                self.rpc_timeout_s, self._receive_timeout, stream_id
            )
        message = self._buffers[stream_id].feed(event.data, end_stream=event.end_stream)
        if message is None:
            return
        self._discard_buffer(stream_id)
        kind = message["type"]
        if local:
            if kind != "response" or stream_id & 2:
                raise RpcError("unexpected_response")
            future = self._pending[stream_id]
            if not future.done():
                if "error" in message:
                    future.set_exception(RpcError(message["error"]))
                else:
                    future.set_result(message["result"])
        elif kind == "event" and stream_id & 2:
            if self.event_handler is not None:
                self.event_handler(message["method"], message["params"])
        elif kind == "request" and not stream_id & 2:
            task = asyncio.create_task(
                self._handle_request(stream_id, message), name="rd-rpc-handler"
            )
            self._tasks[stream_id] = task
            task.add_done_callback(lambda done: self._handler_done(stream_id, done))
        else:
            raise RpcError("unexpected_message")

    def _handler_done(self, stream_id, task):
        self._tasks.pop(stream_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("RPC response failed (%s)", type(task.exception()).__name__)
            self.close(error_code=0x102, reason_phrase="response_failed")

    async def _handle_request(self, stream_id, message):
        try:
            if self.request_handler is None:
                raise RpcError("unsupported_method")
            async with async_timeout(self.rpc_timeout_s):
                result = await self.request_handler(
                    message["method"], message["params"]
                )
            if not isinstance(result, dict):
                raise RpcError("invalid_handler_result")
            response = {"type": "response", "result": result}
        except RpcError as error:
            response = {"type": "response", "error": error.code}
        except TimeoutError:
            response = {"type": "response", "error": "handler_timeout"}
        except Exception as error:  # noqa: BLE001 - sanitize application boundary
            logger.error("RPC handler failed (%s)", type(error).__name__)
            response = {"type": "response", "error": "handler_failed"}
        if not self._rpc_closed:
            try:
                self._send(stream_id, response)
            except RpcError:
                self.close(error_code=0x102, reason_phrase="invalid_handler_result")

    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            if event.alpn_protocol != ALPN or event.early_data_accepted:
                self.close(error_code=0x102, reason_phrase="unsupported_protocol")
                return
            self.rpc_ready = True
        elif isinstance(event, ConnectionTerminated):
            self._cleanup()
        elif isinstance(event, (StreamReset, StopSendingReceived)):
            self._discard_buffer(event.stream_id)
            future = self._pending.get(event.stream_id)
            if future is not None and not future.done():
                future.set_exception(ConnectionError("RPC stream reset"))
            task = self._tasks.get(event.stream_id)
            if task is not None:
                task.cancel()
        elif isinstance(event, (StreamDataReceived, DatagramFrameReceived)):
            if not self.rpc_ready or self._rpc_closed:
                self.close(error_code=0x102, reason_phrase="handshake_required")
                return
            try:
                if isinstance(event, StreamDataReceived):
                    self._stream_data(event)
                elif (
                    len(event.data) <= self.max_datagram_bytes
                    and self.datagram_handler is not None
                ):
                    self.datagram_handler(event.data)
            except RpcError as error:
                self.close(error_code=0x102, reason_phrase=error.code)
            except Exception as error:  # noqa: BLE001 - fail closed without payload diagnostics
                logger.error("QUIC callback failed (%s)", type(error).__name__)
                self.close(error_code=0x102, reason_phrase="callback_failed")
            return
        super().quic_event_received(event)

    def _cleanup(self):
        if self._rpc_closed:
            return
        self._rpc_closed = True
        self.rpc_ready = False
        for stream_id in list(self._buffers):
            self._discard_buffer(stream_id)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("QUIC connection closed"))
        for task in self._tasks.values():
            if task is not asyncio.current_task():
                task.cancel()
        if self.on_disconnect is not None:
            try:
                self.on_disconnect(self)
            except Exception as error:  # noqa: BLE001 - keep transport teardown running
                logger.error(
                    "QUIC disconnect callback failed (%s)", type(error).__name__
                )

    def close(self, error_code=0, reason_phrase=""):
        self._cleanup()
        super().close(error_code=error_code, reason_phrase=reason_phrase)

    def connection_lost(self, exc):
        self._cleanup()
        super().connection_lost(exc)

    async def shutdown(self, timeout_s=3.0):
        self.close()
        try:
            await asyncio.wait_for(self.wait_closed(), timeout_s)
        except asyncio.TimeoutError:
            logger.warning("QUIC close deadline exceeded")
            finish_abandoned_close(self)
        finally:
            cancel_pending_io(self)
            tasks = [
                task
                for task in self._tasks.values()
                if task is not asyncio.current_task()
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

import asyncio
import inspect
import json
import time
from typing import TYPE_CHECKING
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from interfaces import IMessageQueue
from logger import get_logger

# from .control_codec import ControlCodec
from .codec import ControlCodec

logger = get_logger(__name__)


class DrivingProtocol(QuicConnectionProtocol):

    def __init__(
        self,
        session_id: int,
        processed_queue: IMessageQueue,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.session_id = session_id
        self.processed_queue = processed_queue

        self._connected = False
        self._business_task: asyncio.Task | None = None
        self._state_callback = None

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
            message = ControlCodec.decode(data)
            if message["type"] == ControlCodec.MessageType.STATE:
                if self._state_callback:
                    logger.info(f"put data {message}")
                    cb = self._state_callback

                    if inspect.iscoroutinefunction(cb):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(cb(message))
                        except RuntimeError:
                            # 没有运行中的事件循环（极端情况）
                            asyncio.run(cb(message))
                    else:
                        cb(message)
            logger.debug(f"收到服务器消息: {message}")
        except Exception:
            logger.warning(f"收到非法数据: {data!r}")

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
            while self._connected:
                try:
                    data: dict = await asyncio.wait_for(
                        self.processed_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                await self._send_control(data)

                await asyncio.sleep(0.02)  # 50Hz

        except asyncio.CancelledError:
            logger.info("业务循环被取消")

        except Exception:
            logger.exception("业务循环异常")

    # =========================
    # 发送datagram
    # =========================
    def _send_datagram(self, payload: bytes):
        try:
            self._quic.send_datagram_frame(payload)
            self.transmit()
        except Exception:
            logger.exception("发送数据失败")

    # =========================
    # hello / keepalive
    # =========================
    async def _send_hello(self):
        payload = ControlCodec.encode(
            ControlCodec.MessageType.HELLO_DRIVING, self.session_id
        )
        self._send_datagram(payload)
        logger.info(f"身份发送完成 session={self.session_id}")

    # =========================
    # control
    # =========================
    async def _send_control(self, data: dict):
        throttle, brake, steering = data["throttle"], data["brake"], data["steering"]
        payload = ControlCodec.encode(
            ControlCodec.MessageType.CONTROL,
            self.session_id,
            throttle=throttle,
            brake=brake,
            steering=steering,
        )
        self._send_datagram(payload)
        logger.info(
            f"发送指令：throttle = {throttle:.2f}, brake = {brake:.2f}, steering = {steering:.2f}"
        )

    def set_state_callback(self, callback):
        self._state_callback = callback

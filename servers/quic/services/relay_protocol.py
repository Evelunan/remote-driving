import asyncio
import json
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived

from logger import get_logger
from .session_manager import session_manager
from .control_codec import ControlCodec

logger = get_logger(__name__)

ROUTE_TABLE = {
    "hello-car": ("bind", "car"),
    "hello-driving": ("bind", "driving"),
    "control": ("forward", "car"),
    "state": ("forward", "driving"),
    "heartbeat": None,
}


class RelayProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_manager = session_manager
        self._ping_task = None

    def connection_lost(self, exc):
        if self._ping_task:
            self._ping_task.cancel()
        self.session_manager.unbind(self)
        logger.info("connection lost")

    def quic_event_received(self, event):
        if not isinstance(event, DatagramFrameReceived):
            return

        data = event.data
        try:
            msg = ControlCodec.decode(data)
        except Exception as e:
            logger.error(f"decode failed: {e}")
            return

        session_id = msg["session_id"]
        msg_type = msg["type"]
        action = ROUTE_TABLE.get(msg_type)

        if not action:
            return

        op, role = action
        if op == "bind":
            self.session_manager.bind_peer(session_id, role, self)
            return

        peer = self.session_manager.get_peer(session_id, role)
        if peer:
            peer._quic.send_datagram_frame(data)
            peer.transmit()

    async def detect_connection(self):
        while True:
            await asyncio.sleep(5)
            try:
                session_id = self.session_manager.get_session_id(self)
                if not session_id:
                    continue
                # 改为 JSON 格式，避免客户端解析失败
                ping_msg = ControlCodec.encode(ControlCodec.MSG_HEARTBEAT, session_id)

                self._quic.send_datagram_frame(ping_msg)
                self.transmit()  # 确保发送
                # log_info("Ping sent: Connection alive")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Ping failed: {e}")
                self.session_manager.unbind(self)
                break  # 如果失败，unbind 并退出任务

    def connection_made(self, transport):
        super().connection_made(transport)
        self._ping_task = asyncio.create_task(self.detect_connection())  # 启动探测任务

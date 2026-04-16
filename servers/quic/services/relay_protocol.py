import asyncio
import json
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived

from logger import get_logger
from .session_manager import session_manager

# from .control_codec import ControlCodec
from .codec import ControlCodec

logger = get_logger(__name__)


class RelayProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_manager = session_manager
        self._ping_task = None
        self._running = False

    def connection_lost(self, exc):
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
        self.session_manager.unbind(self)
        logger.info("connection lost")

    def quic_event_received(self, event):
        if not isinstance(event, DatagramFrameReceived):
            return

        data = event.data
        try:
            self._handle_datagram(data)
        except Exception as e:
            logger.exception("handle datagram failed")
            return

    async def detect_connection(self):
        self._running = True
        while self._running:
            await asyncio.sleep(5)
            try:
                session_id = self.session_manager.get_session_id(self)
                if not session_id:
                    continue
                ping_msg = ControlCodec.encode(
                    ControlCodec.MessageType.HEARTBEAT, session_id
                )

                self._send_datagram(ping_msg)
                # log_info("Ping sent: Connection alive")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ping failed: {e}")
                self.session_manager.unbind(self)
                break  # 如果失败，unbind 并退出任务

    def connection_made(self, transport):
        super().connection_made(transport)
        self._ping_task = asyncio.create_task(self.detect_connection())  # 启动探测任务

    # =========================
    # 发送datagram
    # =========================
    def _send_datagram(self, payload: bytes):
        try:
            self._quic.send_datagram_frame(payload)
            self.transmit()
        except Exception:
            logger.exception("发送数据失败")

    def _handle_datagram(self, data: bytes):
        msg = ControlCodec.decode_header(data)
        msg_type = msg["type"]
        session = str(msg["session"])
        role = ControlCodec.get_role(msg_type)

        if msg_type in (
            ControlCodec.MessageType.HELLO_CAR,
            ControlCodec.MessageType.HELLO_DRIVING,
        ):
            self._handle_hello(role, session)

        elif msg_type in (
            ControlCodec.MessageType.CONTROL,
            ControlCodec.MessageType.STATE,
        ):
            self._handle_forward(role, session, data)
        elif msg_type == ControlCodec.MessageType.HEARTBEAT:
            self._handle_heartbeat(session)

    def _handle_hello(self, role, session):
        self.session_manager.bind_peer(session_id=session, role=role, protocol=self)
        logger.info(f"[bind] {role} -> session {session}")

    def _handle_forward(self, role, session, data):
        peer = self.session_manager.get_peer(session, role)
        if not peer:
            logger.warning(f"[drop] no peer: {role} session={session}")
            return
        peer._send_datagram(data)

    def _handle_heartbeat(self, session):
        logger.debug(f"[heartbeat] session={session}")

import asyncio
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import ConnectionTerminated, DatagramFrameReceived

from logger import get_logger
from .session_manager import session_manager

from .codec import ControlCodec

logger = get_logger(__name__)


class RelayProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_manager = session_manager
        self._ping_task = None
        self._running = False

    def connection_lost(self, exc):
        self._cleanup()
        super().connection_lost(exc)

    def _cleanup(self):
        """A QUIC connection can end without closing the shared UDP socket."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
        self.session_manager.unbind(self)

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self._cleanup()
        if not isinstance(event, DatagramFrameReceived):
            super().quic_event_received(event)
            return

        data = event.data
        try:
            self._handle_datagram(data)
        except (ValueError, KeyError):
            logger.debug("Dropping invalid datagram")

    async def detect_connection(self):
        self._running = True
        while self._running:
            await asyncio.sleep(5)
            try:
                session_id = self.session_manager.get_session_id(self)
                if not session_id:
                    continue
                ping_msg = ControlCodec.encode(
                    ControlCodec.MessageType.HEARTBEAT, int(session_id)
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
        # All legacy message types use a full fixed-size packet. A header alone
        # must never register a peer or be forwarded to another connection.
        msg = ControlCodec.decode(data)
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
        if not self.session_manager.bind_peer(session_id=session, role=role, protocol=self):
            logger.warning("Rejected conflicting session binding")
            return
        logger.info(f"[bind] {role} -> session {session}")

    def _handle_forward(self, role, session, data):
        if self.session_manager.get_peer(session, role) is not self:
            logger.debug("Dropping datagram from an unbound session or role")
            return
        target_role = {"driving": "car", "car": "driving"}[role]
        peer = self.session_manager.get_peer(session, target_role)
        if not peer:
            logger.debug("No destination peer for session %s", session)
            return
        peer._send_datagram(data)

    def _handle_heartbeat(self, session):
        logger.debug(f"[heartbeat] session={session}")

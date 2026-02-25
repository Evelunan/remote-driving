import asyncio
import json
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived

from logger import get_logger
from .session_manager import session_manager

logger = get_logger(__name__)


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
        session_id, role, msg_data = None, None, event.data
        try:
            msg = json.loads(msg_data.decode())
            session_id = str(msg.get("session_id")) if msg.get("session_id") else None
            role = msg.get("type")
        except Exception as e:
            logger.error(f"invalid message: {e}")

        if not session_id or not role:
            return

        if role.startswith("hello"):
            self.session_manager.bind_peer(
                session_id, "car" if role == "hello-car" else "driving", self
            )
            logger.info(f"bind {role} to session {session_id}")
            return

        # ---- 处理 heartbeat（只 log，不转发）----
        if role in ("heartbeat", "probe"):
            logger.info(f"Received heartbeat from {session_id} msg {msg_data}")
            return

        # ---- 正常转发阶段 ----
        if role == "control":
            peer = self.session_manager.get_peer(session_id, "car")
        elif role == "state":
            peer = self.session_manager.get_peer(session_id, "driving")
        else:
            return

        if peer:
            peer._quic.send_datagram_frame(msg_data)
            peer.transmit()

    async def detect_connection(self):
        while True:
            await asyncio.sleep(5)
            try:
                # 改为 JSON 格式，避免客户端解析失败
                ping_msg = json.dumps({"type": "ping"}).encode()
                self._quic.send_datagram_frame(ping_msg)
                self.transmit()  # 确保发送
                # log_info("Ping sent: Connection alive")
            except Exception as e:
                logger.error(f"Ping failed: {e}")
                self.session_manager.unbind(self)
                break  # 如果失败，unbind 并退出任务

    def connection_made(self, transport):
        super().connection_made(transport)
        self._ping_task = asyncio.create_task(self.detect_connection())  # 启动探测任务

import asyncio
import json
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from logger import get_logger

logger = get_logger(__name__)


class DrivingProtocol(QuicConnectionProtocol):
    def __init__(self, session_id, packet_queue, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.packet_queue = packet_queue
        self._business_task = None
        self._connected = False

    def connection_made(self, transport):
        """UDP 插槽建立时的回调"""
        super().connection_made(transport)
        self._connected = True
        logger.info("QUIC传输已建立，等待握手完成...")

    def quic_event_received(self, event):
        """监听 QUIC 事件"""
        if isinstance(event, ConnectionTerminated):
            logger.warning(f"连接终止: {event.error_code} - {event.reason_phrase}")
            self._connected = False
            if self._business_task:
                self._business_task.cancel()
        elif isinstance(event, DatagramFrameReceived):
            try:
                data = json.loads(event.data.decode())
                logger.info(f"收到服务器消息: {data}")
            except json.JSONDecodeError:
                logger.warning(f"收到无效JSON数据: {event.data}")

    async def start_business(self):
        """启动业务逻辑：Hello + Control Loop"""
        try:
            await self.send_hello()
            await self.send_controls_loop()
        except asyncio.CancelledError:
            logger.info("业务任务因连接断开而取消")
        except Exception as e:
            logger.error(f"业务任务出错: {e}")

    async def send_hello(self):
        """发送身份绑定消息"""
        hello = {
            "session_id": self.session_id,
            "type": "hello-driving",
        }
        self._quic.send_datagram_frame(json.dumps(hello).encode())
        self.transmit()
        logger.info(f"身份已发送: hello-driving (会话ID: {self.session_id})")

    async def send_controls_loop(self):
        """循环发送控制指令"""
        logger.info("启动控制指令循环...")
        while self._connected:
            try:
                # 从队列获取数据，设置超时以便定期检查连接状态
                ctrl_data = await asyncio.wait_for(self.packet_queue.get(), timeout=1.0)
                
                ctrl = {
                    "session_id": self.session_id,
                    "type": "control",
                    "data": ctrl_data
                }
                self._quic.send_datagram_frame(json.dumps(ctrl).encode())
                self.transmit()
                logger.info(f"控制指令已发送: {ctrl_data}")
            except asyncio.TimeoutError:
                continue  # 继续检查连接状态
            except Exception as e:
                logger.error(f"发送控制指令时出错: {e}")
                break
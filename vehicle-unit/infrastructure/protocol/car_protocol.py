import asyncio
import json
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated

from interfaces.icontroller import IController
from logger import get_logger

logger = get_logger(__name__)

class CarProtocol(QuicConnectionProtocol):
    def __init__(self, session_id, controller: IController, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.controller = controller
        self._business_task = None
        self._connected = False

    def connection_made(self, transport):
        """当底层 UDP 传输建立时调用"""
        super().connection_made(transport)
        self._connected = True
        logger.info("[car] UDP transport established, performing QUIC handshake...")

    def quic_event_received(self, event):
        """监听 QUIC 协议事件"""
        if isinstance(event, ConnectionTerminated):
            logger.warning(f"[car] Connection terminated: {event.error_code} - {event.reason_phrase}")
            #更新状态并取消业务任务
            self._connected = False
            if self._business_task:
                self._business_task.cancel()
                
        elif isinstance(event, DatagramFrameReceived):
            self.handle_datagram(event.data)

    def handle_datagram(self, data: bytes):
        """处理接收到的数据报"""
        try:
            msg = json.loads(data.decode())
            msg_type = msg.get("type")
            
            if msg_type == "control":
                self.process_control_message(msg)
            elif msg_type == "ping":
                # 可选：实现 PING 响应逻辑
                pass
            else:
                logger.warning(f"[car] Ignored message type: {msg_type}")
        except Exception as e:
            logger.error(f"[car] Failed to decode message: {e}", exc_info=True)

    def connection_lost(self, exc):
        """当连接丢失时调用"""
        logger.warning(f"[car] Connection lost event triggered: {exc}")
        # 更新状态并取消任务
        self._connected = False
        if self._business_task:
            self._business_task.cancel()
        super().connection_lost(exc)

    # ================= 业务逻辑方法 =================

    async def start_business(self):
        """启动业务逻辑：发送 Hello + 心跳循环"""
        try:
            await self.send_hello()
            await self.send_heartbeat_loop()
        except asyncio.CancelledError:
            logger.info("[car] Business task cancelled gracefully.")
        except Exception as e:
            logger.error(f"[car] Business task failed with unexpected error: {e}", exc_info=True)

    async def send_hello(self):
        """发送身份绑定消息"""
        # 检查连接状态
        if not self._connected:
            logger.warning("[car] Cannot send hello: connection not established yet.")
            return

        hello = {
            "session_id": self.session_id,
            "type": "hello-car",
        }
        try:
            self._send_datagram(json.dumps(hello).encode())
            logger.info(f"[car] Identity sent: hello-car (Session: {self.session_id})")
        except Exception as e:
            logger.error(f"[car] Failed to send hello message: {e}")

    async def send_heartbeat_loop(self):
        """循环发送心跳包"""
        logger.info("[car] Starting heartbeat loop...")
        while self._connected:
            try:
                heartbeat = {
                    "session_id": self.session_id,
                    "role": "car",
                    "type": "heartbeat",
                    "timestamp": int(asyncio.get_event_loop().time())
                }
                
                self._send_datagram(json.dumps(heartbeat).encode())
                
                for _ in range(30): # 30 * 0.1s = 3s
                    if not self._connected:
                        break
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                logger.error(f"[car] Error during heartbeat sending: {e}")
                break
        
        logger.info("[car] Heartbeat loop stopped.")

    def _send_datagram(self, data: bytes):
        """安全的发送数据报方法"""
        if hasattr(self, '_quic') and self._quic is not None:
            try:
                self._quic.send_datagram_frame(data)
                self.transmit()
            except Exception as e:
                logger.error(f"[car] Internal QUIC send error: {e}")
                # 如果发送失败，可能意味着连接已损坏，触发断开逻辑
                self._connected = False
                raise e
        else:
            logger.warning("[car] Attempted to send datagram but QUIC instance is not ready.")
            raise RuntimeError("QUIC instance not ready")

    # ================= 控制消息处理方法 =================

    def process_control_message(self, message: dict):
        """解析并执行控制指令"""
        data = self._parse_data_field(message.get('data'))
        if data is None:
            return
            
        linear, angular = self._extract_control_values(data)
        logger.info(f"[car] Applying control values: linear={linear}, angular={angular}")
        
        try:
            # 实际控制逻辑在这里实现
            self.controller.send_command(linear, angular)
        except Exception as e:
            logger.error(f"[car] Failed to execute control command: {e}")

    def _parse_data_field(self, data_field):
        """统一解析数据字段（支持字符串或字典）"""
        if isinstance(data_field, str):
            return self._parse_json_string(data_field)
        elif isinstance(data_field, dict):
            return data_field
        else:
            logger.warning(f"[car] Unexpected data type in control message: {type(data_field)}")
            return None

    def _extract_control_values(self, data: dict):
        """提取线速度和角速度"""
        linear = float(data.get('linear', 0.0))
        angular = float(data.get('angular', 0.0))
        return linear, angular

    def _parse_json_string(self, json_str: str):
        """解析 JSON 字符串"""
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logger.error(f"[car] Failed to parse data JSON: {json_str}")
            return None
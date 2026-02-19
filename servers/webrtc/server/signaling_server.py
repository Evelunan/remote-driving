import asyncio
from websockets import ConnectionClosed
import websockets

from logger import get_logger
from config.loaders import get_server_config
from services import RoomManager

logger = get_logger(__name__)


class SignalingServer:
    """
    WebRTC 信令服务器

    特性:
        - 基于房间的对等端连接
        - 支持配置化
        - 完整的日志记录
        - 优雅关闭
        - 可扩展的消息处理
    """
    def __init__(self):
        self.config = get_server_config()
        self.manager = RoomManager(max_peers_per_room=self.config.max_peers_per_room)
        self._server = None
    
    async def _handle_connection(self, websocket) ->None:
        """处理 WebSocket 连接"""
        peer = await self.manager.register_peer(websocket)
        try: 
            async for message in websocket:
                await self.manager.handle_message(peer, message)
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"处理对等端 {peer.id} 时出错: {e}")
        finally:
            await self.manager.unregister_peer(peer)        
    
    async def start(self) -> None:
        """启动服务器"""
        self._server = await websockets.serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            ping_interval=self.config.ping_interval,
            ping_timeout=self.config.ping_timeout,
            max_size=self.config.max_message_size,
        )
        logger.info(f"信令服务器已启动: ws://{self.config.host}:{self.config.port}")
            
    async def stop(self) -> None:
        """停止服务器"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # 关闭所有连接
        for peer in list(self.manager.peers.values()):
            await peer.close(1001, "服务器关闭")

        logger.info("信令服务器已停止")

    async def run_forever(self) -> None:
        """运行直到被中断"""
        await self.start()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()


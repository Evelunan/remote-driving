import asyncio
from PyQt6.QtCore import QThread, pyqtSignal

from infrastructure.video import VideoReceiver
from logger import get_logger

logger = get_logger(__name__)


class VideoReceiverThread(QThread):
    """视频接收线程"""

    connection_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    # 新增：详细状态信号
    status_changed = pyqtSignal(str, str)  # (status_code, message)

    def __init__(self, receiver: VideoReceiver):
        super().__init__()
        self._receiver = receiver
        self._loop = None
        self._stopping = False
        # 设置状态回调
        self._receiver.set_status_callback(self._on_status_changed)

    def _on_status_changed(self, status: str, message: str):
        """状态变化回调（在异步线程中调用）"""
        if not self._stopping:
            try:
                self.status_changed.emit(status, message)
            except Exception:
                pass  # 忽略信号发送失败

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self.connection_changed.emit(True)
            self._loop.run_until_complete(self._receiver.start())
        except asyncio.CancelledError:
            logger.debug("视频接收任务被取消")
        except Exception as e:
            if not self._stopping:
                self.error_occurred.emit(str(e))
        finally:
            try:
                self.connection_changed.emit(False)
            except Exception:
                pass
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    def request_stop(self):
        self._stopping = True
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._receiver.stop(), self._loop)
            except Exception as e:
                logger.debug(f"请求停止视频接收器时出错: {e}")

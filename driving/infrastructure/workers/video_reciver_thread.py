import asyncio
from typing import Optional
from PyQt6.QtCore import QThread, pyqtSignal

from infrastructure.video import WebRTCReceiver
from logger import get_logger

logger = get_logger(__name__)


class WebRTCReceiverThread(QThread):
    """视频接收线程 - 修复版"""

    connection_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    status_changed = pyqtSignal(str, str)  # (status_code, message)

    def __init__(self, receiver: WebRTCReceiver):
        super().__init__()
        self._receiver = receiver
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._main_task: Optional[asyncio.Task] = None

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
            # 启动 WebRTC
            self._loop.create_task(self._receiver.start())
            # 保持事件循环运行
            self._loop.run_forever()

        except Exception as e:
            if not self._stopping:
                self.error_occurred.emit(str(e))
        finally:
            self.connection_changed.emit(False)
            try:
                self._loop.run_until_complete(self._receiver.close())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    def request_stop(self, timeout: float = 5.0):
        self._stopping = True

        if self._loop and self._loop.is_running():
            try:
                # 提交关闭任务
                future = asyncio.run_coroutine_threadsafe(
                    self._receiver.close(), self._loop
                )
                
                # ✅ 等待关闭完成（关键）
                future.result(timeout=timeout)

            except Exception as e:
                logger.warning(f"等待关闭超时或异常: {e}")

            finally:
                # 停止事件循环
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except Exception:
                    pass

        # ✅ 等待线程退出（关键）
        self.wait(int(timeout * 1000))

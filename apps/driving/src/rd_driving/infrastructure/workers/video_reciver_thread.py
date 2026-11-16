"""Qt worker owns one asyncio session; stopping never blocks the GUI thread."""

import asyncio
import threading

from PyQt6.QtCore import QThread, pyqtSignal
from rd_core.logging import get_logger

logger = get_logger(__name__)


class WebRTCReceiverThread(QThread):
    connection_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    status_changed = pyqtSignal(str, str)

    def __init__(self, receiver):
        super().__init__()
        self._receiver = receiver
        self._loop = None
        self._main_task = None
        self._stop_requested = threading.Event()
        self._receiver.set_status_callback(self._on_status_changed)

    def _on_status_changed(self, status, message):
        if self._stop_requested.is_set():
            return
        self.status_changed.emit(status, message)
        if status == "joined":
            self.connection_changed.emit(True)
        elif status in {"connecting", "disconnected"}:
            self.connection_changed.emit(False)

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        try:
            if not self._stop_requested.is_set():
                await self._receiver.start()
        finally:
            await self._receiver.close()

    def run(self):
        try:
            asyncio.run(self._main())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - sanitize the thread boundary
            logger.error("Video worker failed (%s)", type(exc).__name__)
            self.error_occurred.emit("视频服务失败：" + type(exc).__name__)
        finally:
            self._loop = None
            self._main_task = None
            self.connection_changed.emit(False)

    def request_stop(self):
        self._stop_requested.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel)
            except RuntimeError:
                pass  # event loop already completed and closed

    def _cancel(self):
        if (
            self._main_task is not None
            and not self._main_task.done()
            and not self._main_task.cancelling()
        ):
            self._main_task.cancel()

"""Qt adapter around the GUI-independent input and authenticated control service."""

import asyncio
import threading

from PyQt6.QtCore import QThread
from rd_core.logging import get_logger
from rd_driving.app.input_service import DrivingInputService

logger = get_logger(__name__)

STATUS_TEXT = {
    "connecting": "连接中",
    "registered": "已认证",
    "waiting_device": "等待控制设备",
    "waiting_input": "等待设备输入",
    "waiting_neutral": "请将控制输入归零",
    "waiting_video": "主画面不可用，控制已禁止",
    "video_unavailable": "主画面失效，控制已撤销",
    "video_vehicle_mismatch": "画面与控制车辆不匹配",
    "waiting_execution": "等待中立执行确认",
    "active": "控制已授权",
    "revoked": "控制授权已撤销",
    "error": "连接或授权失败",
    "disconnected": "离线",
}


class ControlSystemThread(QThread):
    def __init__(self, signal_bridge, settings, *, video_gate=None):
        super().__init__()
        self._bridge, self.settings = signal_bridge, settings
        self._loop = self._main_task = None
        self._stop_requested = threading.Event()
        self.service = None
        self.video_gate = video_gate

    def _safe_emit(self, signal_name, *args):
        if not self._stop_requested.is_set() and self._bridge is not None:
            getattr(self._bridge, signal_name).emit(*args)

    def _status(self, state, detail=""):
        online = state in {
            "registered",
            "waiting_device",
            "waiting_input",
            "waiting_neutral",
            "waiting_video",
            "waiting_execution",
            "active",
        }
        self._safe_emit(
            "quic_status_changed",
            online,
            "控制协议不兼容，请同步升级"
            if detail == "protocol_upgrade_required"
            else STATUS_TEXT.get(state, "状态未知"),
        )
        if state != "active":
            self._safe_emit("control_confirmation_expired")

    def _execution(self, sample, execution, elapsed_ms):
        self._safe_emit(
            "control_data_updated",
            sample.throttle * 100,
            sample.brake * 100,
            sample.steering * 100,
            float(elapsed_ms),
        )

    def _telemetry(self, packet):
        self._safe_emit(
            "vehicle_status_updated",
            {
                "linear_mps": packet.payload.linear_mps,
                "angular_radps": packet.payload.angular_radps,
                "source_time_ms": packet.source_time_ms,
            },
        )

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        if self._stop_requested.is_set():
            return
        self.service = DrivingInputService(
            self.settings,
            on_usb=lambda *args: self._safe_emit("usb_status_changed", *args),
            on_status=self._status,
            on_execution=self._execution,
            on_telemetry=self._telemetry,
            video_gate=self.video_gate,
        )
        await self.service.run()

    def run(self):
        try:
            asyncio.run(self._main())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - thread boundary, sanitized diagnostics
            logger.error("Control worker failed (%s)", type(exc).__name__)
        finally:
            self._status("disconnected")
            self._loop = self._main_task = None

    def request_usb_connect(self):
        """Dispatch GUI requests to the asyncio loop that owns the device."""
        loop = self._loop
        if self._stop_requested.is_set() or loop is None or self.service is None:
            return False
        try:
            loop.call_soon_threadsafe(self._request_usb_connect)
        except RuntimeError:
            return False
        return True

    def _request_usb_connect(self):
        if not self._stop_requested.is_set() and self.service is not None:
            self.service.request_usb_connect()

    def request_stop(self):
        self._stop_requested.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel)
            except RuntimeError:
                pass

    def request_control_reconnect(self):
        loop = self._loop
        if self._stop_requested.is_set() or loop is None or self.service is None:
            return False
        try:
            loop.call_soon_threadsafe(self._request_control_reconnect)
        except RuntimeError:
            return False
        return True

    def _request_control_reconnect(self):
        if not self._stop_requested.is_set() and self.service is not None:
            self.service.request_control_reconnect()

    def _cancel(self):
        if (
            self._main_task is not None
            and not self._main_task.done()
            and not self._main_task.cancelling()
        ):
            self._main_task.cancel()

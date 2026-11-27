"""Qt adapter around the GUI-independent input and authenticated control service."""

import asyncio
import queue
import sys
import threading

from PyQt6.QtCore import QThread
from rd_core.async_compat import Runner
from rd_core.logging import get_logger
from rd_driving.app.input_service import DrivingInputService

logger = get_logger(__name__)

STATUS_TEXT = {
    "connecting": "连接中",
    "registered": "已认证",
    "idle": "未开始",
    "starting": "准备发送",
    "retrying": "等待自动恢复控制",
    "waiting_channel": "等待数据通道",
    "waiting_vehicle": "等待车端连接",
    "paused": "控制已暂停",
    "waiting_device": "等待控制设备",
    "waiting_input": "等待设备输入",
    "waiting_neutral": "请将控制输入归零",
    "waiting_control_window": "等待车端控制窗口",
    "active": "发送中",
    "channel_busy": "数据通道已被占用",
    "driver_already_connected": "已有驾驶端连接",
    "vehicle_changed": "受控车辆已更换，请核对后开始控制",
    "error": "连接或授权失败",
    "disconnected": "离线",
}


class ControlSystemThread(QThread):
    def __init__(self, signal_bridge, settings):
        super().__init__()
        self._bridge, self.settings = signal_bridge, settings
        self._loop = self._main_task = None
        self._stop_requested = threading.Event()
        self._control_requests = queue.SimpleQueue()
        self.service = None

    def _safe_emit(self, signal_name, *args):
        if not self._stop_requested.is_set() and self._bridge is not None:
            getattr(self._bridge, signal_name).emit(*args)

    def _status(self, state, detail=""):
        text = STATUS_TEXT.get(state, "状态未知")
        if state == "paused":
            text = {
                "access_denied": "控制权限被拒绝",
                "role_denied": "当前身份没有控制权限",
                "execution_failed": "车端执行失败",
                "protocol_upgrade_required": "控制协议不兼容",
            }.get(detail, text)
        self._safe_emit("control_status_changed", state, text)
        if state == "active" and detail:
            self._safe_emit("control_target_changed", detail)
        if state != "active":
            self._safe_emit("control_confirmation_expired")

    def _connection_status(self, online, detail=""):
        self._safe_emit(
            "quic_status_changed",
            online,
            "控制协议不兼容，请同步升级"
            if detail == "protocol_upgrade_required"
            else STATUS_TEXT.get(detail, STATUS_TEXT["disconnected"]),
        )

    def _execution(self, sample, execution, elapsed_ms):
        budget_ms = self.settings.control.command_timeout_ms
        client = self.service.client if self.service is not None else None
        connection = client.connection if client is not None else None
        if connection is not None:
            budget_ms = min(budget_ms, connection.max_control_age_ms)
        self._safe_emit("control_data_updated", {
            "sample": sample,
            "elapsed_ms": float(elapsed_ms),
            "deadline": sample.sampled_at + budget_ms / 1000,
        })

    def _telemetry(self, packet):
        self._safe_emit(
            "vehicle_status_updated",
            {
                "linear_mps": packet.payload.linear_mps,
                "angular_radps": packet.payload.angular_radps,
                "source_time_ms": packet.source_time_ms,
            },
        )

    def _vehicle_status(self, packet, deadline):
        self._safe_emit("vehicle_status_updated", {
            "status": packet.payload,
            "source_time_ms": packet.source_time_ms,
            "deadline": deadline,
        })

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        if self._stop_requested.is_set():
            return
        self.service = DrivingInputService(
            self.settings,
            on_usb=lambda *args: self._safe_emit("usb_status_changed", *args),
            on_local_input=lambda state: self._safe_emit("local_input_changed", state),
            on_status=self._status,
            on_connection_status=self._connection_status,
            on_execution=self._execution,
            on_telemetry=self._telemetry,
            on_vehicle_status=self._vehicle_status,
        )
        self._apply_pending_control_requests()
        await self.service.run()

    def run(self):
        try:
            # The Windows Qt worker exchanges small UDP control packets continuously.
            factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
            with Runner(loop_factory=factory) as runner:
                runner.run(self._main())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - thread boundary, sanitized diagnostics
            logger.error("Control worker failed (%s)", type(exc).__name__)
        finally:
            self._connection_status(False, "disconnected")
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

    def request_start_control(self):
        return self._dispatch_control_request(DrivingInputService.request_start_control)

    def request_stop_control(self):
        return self._dispatch_control_request(DrivingInputService.request_stop_control)

    def _dispatch_control_request(self, action):
        if self._stop_requested.is_set() or self.isFinished():
            return False
        self._control_requests.put(action)
        loop = self._loop
        if loop is None:
            return True  # The worker drains requests once the input service is ready.
        try:
            loop.call_soon_threadsafe(self._apply_pending_control_requests)
        except RuntimeError:
            return False
        return True

    def _apply_pending_control_requests(self):
        while not self._stop_requested.is_set() and self.service is not None:
            try:
                action = self._control_requests.get_nowait()
            except queue.Empty:
                return
            self.service.apply_control_request(action)

    def _cancel(self):
        task, self._main_task = self._main_task, None
        if task is not None and not task.done():
            task.cancel()

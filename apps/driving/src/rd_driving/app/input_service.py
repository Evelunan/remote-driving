"""Input acquisition, mapping and v2 control, independent of the GUI runtime."""

import asyncio

from rd_core.async_compat import TaskGroup
from rd_core.logging import get_logger
from rd_core.samples import RawInput

from rd_driving.infrastructure.communication import LatestValueQueue
from rd_driving.infrastructure.device import USBConnector
from rd_driving.infrastructure.processing import AnalogCalculator, DataProcessor

from .control_client import DrivingControlClient

logger = get_logger(__name__)


class DrivingInputService:
    def __init__(
        self,
        settings,
        *,
        on_usb=None,
        on_status=None,
        on_execution=None,
        on_telemetry=None,
        device_factory=None,
        video_gate=None,
    ):
        self.settings, self.on_usb = settings, on_usb
        self.on_status = on_status
        self._usb_online = False
        self._control_state, self._control_detail = "disconnected", ""
        self._device_factory = device_factory or USBConnector
        self.raw, self.processed = LatestValueQueue(), LatestValueQueue()
        self._usb_retry = asyncio.Event()
        self._usb_waiting = False
        self._control_task = None
        self._control_restart_requested = False
        self.client = DrivingControlClient(
            settings,
            self.processed,
            on_status=self._control_status,
            on_execution=on_execution,
            on_telemetry=on_telemetry,
            video_gate=video_gate,
        )

    def _usb_status(self, online, message):
        self._usb_online = online
        if self.on_usb is not None:
            self.on_usb(online, message)
        if self._control_state in {"waiting_input", "waiting_neutral"}:
            self._control_status(self._control_state, self._control_detail)

    def _control_status(self, state, detail=""):
        self._control_state, self._control_detail = state, detail
        if not self._usb_online and state in {"waiting_input", "waiting_neutral"}:
            state = "waiting_device"
        if self.on_status is not None:
            self.on_status(state, detail)

    def request_usb_connect(self):
        """Request one attempt on the service loop; ignore busy/online clicks."""
        if not self._usb_waiting:
            return False
        self._usb_waiting = False
        self._usb_retry.set()
        return True

    async def _usb(self):
        device = self._device_factory(
            self.settings.usb.vendor_id, self.settings.usb.product_id
        )
        device.set_data_callback(self.raw.put)
        try:
            while True:
                try:
                    self._usb_status(False, "连接中")
                    await device.connect()
                    self._usb_status(True, "已连接")
                    while device.is_connected():
                        await asyncio.sleep(0.1)
                except (ConnectionError, OSError) as exc:
                    logger.warning(
                        "Input device unavailable (%s); waiting for manual retry",
                        type(exc).__name__,
                    )
                finally:
                    await self.raw.put(RawInput.capture(None))
                    await device.disconnect()
                self._usb_waiting = True
                self._usb_status(False, "未连接")
                await self._usb_retry.wait()
                self._usb_retry.clear()
        finally:
            self._usb_waiting = False
            self._usb_status(False, "离线")

    async def _process(self):
        processor = DataProcessor(self.processed, AnalogCalculator())
        while True:
            await processor.process(await self.raw.get())

    def request_control_reconnect(self):
        """Restart only the control client, after its lease/connection cleanup."""
        if (
            self._control_task is None
            or self._control_task.done()
            or self._control_restart_requested
        ):
            return False
        self._control_restart_requested = True
        self._control_task.cancel()
        return True

    async def _control(self):
        try:
            while True:
                self._control_restart_requested = False
                self._control_task = asyncio.create_task(self.client.run())
                try:
                    await asyncio.wait({self._control_task})
                except asyncio.CancelledError:
                    if not self._control_restart_requested:
                        self._control_task.cancel()
                    await asyncio.gather(self._control_task, return_exceptions=True)
                    raise
                if self._control_restart_requested and self._control_task.cancelled():
                    continue
                self._control_task.result()
                return
        finally:
            self._control_task = None
            self._control_restart_requested = False

    async def run(self):
        async with TaskGroup() as tasks:
            tasks.create_task(self._usb(), name="driving-usb")
            tasks.create_task(self._process(), name="driving-process-input")
            tasks.create_task(self._control(), name="driving-control")

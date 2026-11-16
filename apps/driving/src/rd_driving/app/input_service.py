"""Input acquisition, mapping and v2 control, independent of the GUI runtime."""

import asyncio

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
        self._device_factory = device_factory or USBConnector
        self.raw, self.processed = LatestValueQueue(), LatestValueQueue()
        self.client = DrivingControlClient(
            settings,
            self.processed,
            on_status=on_status,
            on_execution=on_execution,
            on_telemetry=on_telemetry,
            video_gate=video_gate,
        )

    def _usb_status(self, online, message):
        if self.on_usb is not None:
            self.on_usb(online, message)

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
                    self._usb_status(True, "在线")
                    while device.is_connected():
                        await asyncio.sleep(0.1)
                except (ConnectionError, OSError) as exc:
                    logger.warning("Input device disconnected (%s)", type(exc).__name__)
                finally:
                    await self.raw.put(RawInput.capture(None))
                    self._usb_status(False, "未连接")
                    await device.disconnect()
                await asyncio.sleep(self.settings.network.reconnect_delay_s)
        finally:
            self._usb_status(False, "离线")

    async def _process(self):
        processor = DataProcessor(self.processed, AnalogCalculator())
        while True:
            await processor.process(await self.raw.get())

    async def run(self):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._usb(), name="driving-usb")
            tasks.create_task(self._process(), name="driving-process-input")
            tasks.create_task(self.client.run(), name="driving-control")

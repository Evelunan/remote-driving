import asyncio
import inspect
import usb.core, usb.util
from typing import Callable, Optional, Awaitable

from interfaces import IInputDevice

from logger import get_logger

logger = get_logger(__name__)


class USBConnector(IInputDevice):
    def __init__(self, vendor_id: int, product_id: int, endpoint_in: int):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.endpoint_in = endpoint_in

        self.device: Optional[usb.core.Device] = None
        self.interface = None
        self.endpoint = None

        self.data_callback: Callable[[bytes], object] = lambda x: None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        self.device = usb.core.find(
            idVendor=self.vendor_id,
            idProduct=self.product_id,
        )

        if self.device is None:
            raise ConnectionError("USB device not found")

        self.device.set_configuration()

        cfg = self.device.get_active_configuration()

        # 查找 HID interface + endpoint
        for intf in cfg:
            if intf.bInterfaceClass == 3:  # HID
                for ep in intf:
                    if ep.bEndpointAddress == self.endpoint_in:
                        self.interface = intf
                        self.endpoint = ep
                        break

        if self.endpoint is None:
            raise RuntimeError("HID endpoint not found")

        # Linux 下需要 detach kernel driver
        try:
            if self.device.is_kernel_driver_active(self.interface.bInterfaceNumber):
                self.device.detach_kernel_driver(self.interface.bInterfaceNumber)
        except Exception:
            pass

        self._running = True
        self._task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        while self._running:
            try:
                data = await asyncio.to_thread(
                    self.endpoint.read,
                    self.endpoint.wMaxPacketSize,
                    100,  # timeout ms
                )

                result = self.data_callback(bytes(data))
                if inspect.iscoroutine(result):
                    await result

            except usb.core.USBTimeoutError:
                pass
            except Exception as e:
                logger.error("USB error: %s", e)
                await asyncio.sleep(0.1)

    def set_data_callback(
        self, callback: Callable[[bytes], None | Awaitable[None]]
    ) -> None:
        self.data_callback = callback

    async def disconnect(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self.device:
            usb.util.dispose_resources(self.device)

    def is_connected(self) -> bool:
        return self._running and self.device is not None

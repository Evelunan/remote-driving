import asyncio
import inspect
import usb.core
import usb.util
from typing import Callable, Optional, Awaitable

from interfaces import IInputDevice
from logger import get_logger

logger = get_logger(__name__)


class USBConnector(IInputDevice):
    def __init__(self, vendor_id: int, product_id: int):
        self.vendor_id = vendor_id
        self.product_id = product_id

        self.device: Optional[usb.core.Device] = None
        self.interface = None
        self.endpoint_in = None

        self.data_callback: Callable[[bytes], object] = lambda x: None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ==========================
    # CONNECT
    # ==========================
    async def connect(self) -> None:
        logger.info(
            "Searching USB device %04x:%04x",
            self.vendor_id,
            self.product_id
        )

        self.device = usb.core.find(
            idVendor=self.vendor_id,
            idProduct=self.product_id,
        )

        if self.device is None:
            raise ConnectionError("USB device not found")

        self.device.set_configuration()
        cfg = self.device.get_active_configuration()

        self.interface = None
        self.endpoint_in = None

        # 正确遍历结构
        for intf in cfg:
            for ep in intf:
                if usb.util.endpoint_direction(
                    ep.bEndpointAddress
                ) == usb.util.ENDPOINT_IN:
                    self.interface = intf
                    self.endpoint_in = ep
                    break
            if self.endpoint_in:
                break

        if self.endpoint_in is None:
            raise RuntimeError("IN endpoint not found")

        # Linux 需要 detach
        try:
            if self.device.is_kernel_driver_active(
                self.interface.bInterfaceNumber
            ):
                self.device.detach_kernel_driver(
                    self.interface.bInterfaceNumber
                )
        except Exception:
            pass

        # Claim interface（重要）
        usb.util.claim_interface(
            self.device,
            self.interface.bInterfaceNumber
        )

        logger.info(
            "USB connected: interface=%d endpoint=0x%02x",
            self.interface.bInterfaceNumber,
            self.endpoint_in.bEndpointAddress
        )

        self._running = True
        self._task = asyncio.create_task(self._read_loop())

    # ==========================
    # READ LOOP
    # ==========================
    async def _read_loop(self):
        while self._running:
            try:
                data = await asyncio.to_thread(
                    self.endpoint_in.read,
                    self.endpoint_in.wMaxPacketSize,
                    100,  # timeout ms
                )

                result = self.data_callback(bytes(data))
                if inspect.iscoroutine(result):
                    await result

            except usb.core.USBTimeoutError:
                # 正常现象
                pass

            except usb.core.USBError as e:
                logger.error("USBError: %s", e)
                await asyncio.sleep(0.2)

            except Exception as e:
                logger.exception("Unexpected USB error: %s", e)
                await asyncio.sleep(0.5)

    # ==========================
    # CALLBACK
    # ==========================
    def set_data_callback(
        self, callback: Callable[[bytes], None | Awaitable[None]]
    ) -> None:
        self.data_callback = callback

    # ==========================
    # DISCONNECT
    # ==========================
    async def disconnect(self) -> None:
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self.device and self.interface:
            try:
                usb.util.release_interface(self.device, self.interface.bInterfaceNumber)
            except Exception:
                pass

        if self.device:
            usb.util.dispose_resources(self.device)

        logger.info("USB disconnected")

    # ==========================
    def is_connected(self) -> bool:
        return self._running and self.device is not None

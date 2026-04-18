import asyncio
import inspect
import usb1
from typing import Callable, Optional, Awaitable

from interfaces import IInputDevice
from logger import get_logger

logger = get_logger(__name__)


class USBConnector(IInputDevice):

    def __init__(self, vendor_id: int, product_id: int):
        self.vendor_id = vendor_id
        self.product_id = product_id

        self.context: Optional[usb1.USBContext] = None
        self.handle: Optional[usb1.USBDeviceHandle] = None

        self.interface_number: Optional[int] = None
        self.endpoint_in: Optional[int] = None
        self.max_packet_size = 64

        self.data_callback: Callable[[bytes], object] = lambda x: None

        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ==========================
    # CONNECT
    # ==========================
    async def connect(self) -> None:

        logger.info("Searching USB device %04x:%04x", self.vendor_id, self.product_id)

        self.context = usb1.USBContext()

        device = None
        for dev in self.context.getDeviceList():
            if (
                dev.getVendorID() == self.vendor_id
                and dev.getProductID() == self.product_id
            ):
                device = dev
                break

        if device is None:
            raise ConnectionError("USB device not found")

        self.handle = device.open()

        # 查找 IN endpoint
        for config in device:
            for interface in config:
                for setting in interface:
                    for ep in setting:

                        addr = ep.getAddress()

                        # 判断 IN endpoint
                        if addr & usb1.ENDPOINT_DIR_MASK == usb1.ENDPOINT_IN:

                            self.interface_number = setting.getNumber()
                            self.endpoint_in = addr
                            self.max_packet_size = ep.getMaxPacketSize()
                            break

        if self.endpoint_in is None:
            raise RuntimeError("IN endpoint not found")

        # detach kernel driver (Linux)
        try:
            if self.handle.kernelDriverActive(self.interface_number):
                self.handle.detachKernelDriver(self.interface_number)
        except Exception:
            pass

        self.handle.claimInterface(self.interface_number)

        logger.info(
            "USB connected interface=%d endpoint=0x%02x",
            self.interface_number,
            self.endpoint_in,
        )

        self._running = True
        self._task = asyncio.create_task(self._read_loop())

    # ==========================
    # READ LOOP
    # ==========================
    def _should_process(
        self, data: bytes, last: bytes | None, threshold: int = 0
    ) -> bool:
        v3, v4 = data[3], data[4]

        # 1. 忽略中立状态
        if abs(v3 - 128) <= threshold and abs(v4 - 128) <= threshold:
            return False
        # 2. 忽略微小变化
        # if last is not None:
        #     lv3, lv4 = last[3], last[4]
        #     if abs(v3 - lv3) <= threshold and abs(v4 - lv4) <= threshold:
        #         return False

        return True

    async def _read_loop(self):
        last_data = None
        while self._running:
            try:

                data = await asyncio.to_thread(
                    self.handle.bulkRead,
                    self.endpoint_in,
                    self.max_packet_size,
                    timeout=100,
                )
                data = bytes(data)
                if not self._should_process(data, last_data):
                    continue
                last_data = data
                result = self.data_callback(data)
                if inspect.iscoroutine(result):
                    await result

            except usb1.USBErrorTimeout:
                pass

            except usb1.USBError as e:
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

        if self.handle and self.interface_number is not None:
            try:
                self.handle.releaseInterface(self.interface_number)
            except Exception:
                pass

        if self.handle:
            self.handle.close()

        if self.context:
            self.context.close()

        logger.info("USB disconnected")

    # ==========================
    def is_connected(self) -> bool:
        return self._running and self.handle is not None

import asyncio
import inspect
import usb1
from typing import Callable, Optional, Awaitable
from rd_core.samples import RawInput

from rd_driving.interfaces import IInputDevice
from rd_core.logging import get_logger

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

        self.data_callback: Callable[[RawInput], object] = lambda x: None

        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ==========================
    # CONNECT
    # ==========================
    async def connect(self) -> None:
        await self.disconnect()
        try:
            await self._open()
        except BaseException:
            await self.disconnect()
            raise

    async def _open(self) -> None:

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
        # 中立与未变化的采样也表明输入仍然有效，不能在此过滤。
        return len(data) >= 5

    async def _read_loop(self):
        try:
            await self._read_packets()
        finally:
            self._running = False
            try:
                result = self.data_callback(RawInput.capture(None))
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Failed to notify input invalidation")

    def _read_once(self):
        data = self.handle.bulkRead(self.endpoint_in, self.max_packet_size, timeout=100)
        # Timestamp in the device thread, before an event-loop scheduling delay.
        return RawInput.capture(bytes(data))

    async def _read_packets(self):
        last_data = None
        while self._running:
            try:

                read = asyncio.create_task(asyncio.to_thread(
                    self._read_once,
                ))
                try:
                    sample = await asyncio.shield(read)
                except asyncio.CancelledError:
                    # libusb 的读取超时为 100ms；关闭句柄前等待读取退出。
                    try:
                        await read
                    except Exception:
                        pass
                    raise
                data = sample.data
                if not self._should_process(data, last_data):
                    continue
                last_data = data
                result = self.data_callback(sample)
                if inspect.isawaitable(result):
                    await result

            except usb1.USBErrorTimeout:
                pass

            except usb1.USBError as e:
                if e.value == usb1.ERROR_NO_DEVICE:
                    logger.warning("USB device unplugged.")
                    break
                logger.error("USBError: %s", e)
                await asyncio.sleep(0.2)

            except Exception as e:
                logger.exception("Unexpected USB error: %s", e)
                await asyncio.sleep(0.5)

    # ==========================
    # CALLBACK
    # ==========================
    def set_data_callback(
        self, callback: Callable[[RawInput], None | Awaitable[None]]
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
            except Exception:
                logger.exception("USB reader failed during disconnect")
            self._task = None

        was_connected = self.handle is not None

        if self.handle and self.interface_number is not None:
            try:
                self.handle.releaseInterface(self.interface_number)
            except Exception:
                pass

        if self.handle:
            self.handle.close()
            self.handle = None

        if self.context:
            self.context.close()
            self.context = None

        self.interface_number = None
        self.endpoint_in = None
        self.max_packet_size = 64

        if was_connected:
            logger.info("USB disconnected")

    # ==========================
    def is_connected(self) -> bool:
        return self._running and self.handle is not None

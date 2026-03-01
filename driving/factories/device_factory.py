from interfaces import IInputDevice
from infrastructure.device import USBConnector


class DeviceFactory:
    @staticmethod
    def create_usb_device(
        vendor_id: int, product_id: int, endpoint_in: int
    ) -> IInputDevice:
        return USBConnector(vendor_id, product_id, endpoint_in)

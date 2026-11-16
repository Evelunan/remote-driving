from aioquic.quic.configuration import QuicConfiguration

from rd_driving.interfaces import IInputDevice, IAnalogCalculator, IMessageQueue, IDataProcessor
from rd_driving.infrastructure.device import USBConnector
from rd_driving.infrastructure.processing import AnalogCalculator, DataProcessor
from rd_driving.infrastructure.communication import LatestValueQueue
from rd_driving.app import DrivingDatagram


class ControlFactory:

    @staticmethod
    def create_analog_calculator() -> IAnalogCalculator:
        return AnalogCalculator()

    @staticmethod
    def create_message_queue() -> IMessageQueue:
        return LatestValueQueue()

    @staticmethod
    def create_data_processor(
        processed_queue: IMessageQueue, analog_calculator: IAnalogCalculator = None
    ) -> IDataProcessor:
        if analog_calculator is None:
            analog_calculator = ControlFactory.create_analog_calculator()
        return DataProcessor(processed_queue, analog_calculator)

    @staticmethod
    def create_usb_device(vendor_id, product_id) -> IInputDevice:
        return USBConnector(vendor_id, product_id)

    @staticmethod
    def quic_client_service(
        host,
        port,
        config: QuicConfiguration,
        session_id,
        processed_queue: IMessageQueue,
    ):
        return DrivingDatagram(host, port, config, session_id, processed_queue)

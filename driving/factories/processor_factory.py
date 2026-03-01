from interfaces.processor import IDataProcessor, IAnalogCalculator
from interfaces.communication import IMessageQueue
from infrastructure.processing import DataProcessor, AnalogCalculator


class ProcessorFactory:
    @staticmethod
    def create_analog_calculator() -> IAnalogCalculator:
        return AnalogCalculator()

    @staticmethod
    def create_data_processor(
        packet_queue: IMessageQueue,
        analog_calculator: IAnalogCalculator = None,
        **kwargs
    ) -> IDataProcessor:
        if analog_calculator is None:
            analog_calculator = ProcessorFactory.create_analog_calculator()
        return DataProcessor(packet_queue, analog_calculator, **kwargs)

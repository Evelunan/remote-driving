import asyncio


from factories import VideoFactory
from config.loaders import get_quic_config, get_webrtc_config
from interfaces import IMessageQueue, IDataProcessor
from infrastructure.communication import ControlState
from infrastructure.device import USBConnector
from infrastructure.processing import DataProcessor, AnalogCalculator

from logger import get_logger
from config.loaders import get_quic_config
from app import DrivingDatagram

logger = get_logger(__name__)


class USBConfig:
    """USB 设备配置"""

    # 方向盘设备
    VENDOR_ID = 0x2F24
    PRODUCT_ID = 0x0009
    ENDPOINT_IN = 0x81


async def test_video():
    quic_config = get_quic_config()
    webrtc_config = get_webrtc_config()
    video = VideoFactory.create_video_receiver(
        room="1234",
        server_url=webrtc_config.signaling.url,
        ice_servers=webrtc_config.ice_servers,
    )
    await video.start()


async def run_usb_connector(control_state: ControlState):
    usb_connector = USBConnector(USBConfig.VENDOR_ID, USBConfig.PRODUCT_ID)
    analog_calculator = AnalogCalculator()
    processor = DataProcessor(control_state, analog_calculator)
    usb_connector.set_data_callback(processor.process)
    try:
        await usb_connector.connect()
    except ConnectionError as e:
        logger.warning("USB connector not available: %s", e)
        return
    try:
        await asyncio.Future()  # 保持运行
    finally:
        await usb_connector.disconnect()


# async def run_processor(queue: IMessageQueue, control_state: ControlState):


async def run_quic_client(control_state: ControlState, session_id="1234"):
    config = get_quic_config()
    client = DrivingDatagram(config, session_id, control_state)
    await client.start()


async def main():
    # queue = CommunicationFactory.create_message_queue()
    # packet_queue = asyncio.Queue()
    control_state = ControlState()
    async with asyncio.TaskGroup() as tasks:
        # tasks.create_task(test_video())
        tasks.create_task(run_usb_connector(control_state))
        tasks.create_task(run_quic_client(control_state))


if __name__ == "__main__":
    # asyncio.run(test_video())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("客户端已退出")

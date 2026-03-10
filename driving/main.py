import asyncio


from factories import VideoFactory
from config.loaders import get_quic_config, get_webrtc_config
from interfaces import IMessageQueue
from factories import DeviceFactory, ProcessorFactory, CommunicationFactory
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


async def run_usb_connector(queue: IMessageQueue):
    usb_connector = DeviceFactory.create_usb_device(
        vendor_id=USBConfig.VENDOR_ID,
        product_id=USBConfig.PRODUCT_ID,
        endpoint_in=USBConfig.ENDPOINT_IN,
    )
    usb_connector.set_data_callback(queue.put)
    try:
        await usb_connector.connect()
    except ConnectionError as e:
        logger.warning("USB connector not available: %s", e)
        return
    try:
        await asyncio.Future()  # 保持运行
    finally:
        await usb_connector.disconnect()


async def run_processor(queue: IMessageQueue, packet_queue: asyncio.Queue):
    analog_calculator = ProcessorFactory.create_analog_calculator()
    processor = ProcessorFactory.create_data_processor(packet_queue, analog_calculator)
    while True:
        raw_data = await queue.get()
        await processor.process(raw_data)


async def run_quic_client(packet_queue: asyncio.Queue, session_id="1234"):
    config = get_quic_config()
    client = DrivingDatagram(config, session_id, packet_queue)
    await client.start()


async def main():
    queue = CommunicationFactory.create_message_queue()
    packet_queue = asyncio.Queue()

    async with asyncio.TaskGroup() as tasks:
        # tasks.create_task(test_video())
        tasks.create_task(run_usb_connector(queue))
        tasks.create_task(run_processor(queue, packet_queue))
        tasks.create_task(run_quic_client(packet_queue))


if __name__ == "__main__":
    # asyncio.run(test_video())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("客户端已退出")

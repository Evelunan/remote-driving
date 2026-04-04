# main.py
import asyncio
import os
import multiprocessing as mp
import os
import sys


from application import  VehicleDatagram, MultiCameraManager
from common.utils import *
from logger import get_logger
from config.loaders import (
    get_webrtc_config,
    get_quic_config,
    get_regist_config,
    get_cameras_config,
    get_resolution_config,
    get_vehicle_config,
)
from infrastructure.cameras import USBCamera
from infrastructure.streams import WebRTCStreamer
from infrastructure.compositors import DrivingCompositor
from infrastructure.controller import SerialController

os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["all_proxy"] = ""

logger = get_logger(__name__)



async def start_control(session_id: str):
    quic_config = get_quic_config()
    vehicle_config = get_vehicle_config()
    # controller = SerialController(vehicle_config)
    client = VehicleDatagram(
        config=quic_config,
        session_id=session_id,
        # controller=controller,
        controller=None,
    )
    await client.start()


async def test_multi_camera_manager(room="1234"):
    config = get_webrtc_config()
    streamer = WebRTCStreamer(room, config)
    manager = MultiCameraManager(streamer)
    cameras = get_cameras_config()
    resolution = get_resolution_config()
    for c in cameras:
        id = get_camera_index(vid=c.vid, pid=c.pid)
        camera = USBCamera(
            camera_id=id,
            camera_name=c.name,
        )
        manager.add_camera(camera)

    await manager.start()
    await streamer.start()
    stop_event = asyncio.Event()
    await stop_event.wait()


async def main():
    mp.set_start_method("spawn", force=True)
    try:
        session_id = 1234
        room = "1234"
        logger.info("启动视频推流和控制指令接收...")
        await asyncio.gather(
            # start_stream(room),
            # start_control(session_id),
            test_multi_camera_manager()
        )
    except KeyboardInterrupt:
        logger.info("用户终止程序")
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
    # test_controller()

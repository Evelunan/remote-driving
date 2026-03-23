# main.py
import asyncio
import os
import multiprocessing as mp
import os
import sys


from application import MultiCameraStreamApp, VehicleDatagram
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


async def start_stream(room: str):
    app = MultiCameraStreamApp()
    cameras = get_cameras_config()
    resolution = get_resolution_config()
    for c in cameras:
        id = get_camera_index(vid=c.vid, pid=c.pid)
        camera = USBCamera(
            camera_id=id,
            width=resolution.width,
            height=resolution.height,
            fps=resolution.fps,
            camera_name=c.name,
        )
        app.add_camera(camera)

    streamer = WebRTCStreamer(room=room, config=get_webrtc_config())
    app.set_stream_encoder(streamer)

    compositor = DrivingCompositor(width=resolution.width, height=resolution.height)
    app.set_frame_compositor(compositor)

    await app.run_stream(preview=True)


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


async def main():
    mp.set_start_method("spawn", force=True)
    try:
        session_id = 1234
        room = "1234"
        logger.info("启动视频推流和控制指令接收...")
        await asyncio.gather(
            start_stream(room),
            start_control(session_id),
        )

    except KeyboardInterrupt:
        logger.info("用户终止程序")
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

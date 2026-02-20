# main.py
import asyncio
import os
import multiprocessing as mp
import os


from application import MultiCameraStreamApp
from common.utils import *
from logger import get_logger
from config.loaders import (
    get_webrtc_config,
    get_quic_config,
    get_regist_config,
    get_cameras_config,
    get_resolution_config,
)
from impl.cameras import USBCamera
from impl.streams import WebRTCStreamer
from impl.compositors import DrivingCompositor

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

    await app.run_stream(preview=False)


async def main():
    mp.set_start_method("spawn", force=True)
    logger.info("启动视频推流和控制指令接收...")
    await asyncio.gather(start_stream("1234"))


if __name__ == "__main__":
    asyncio.run(main())

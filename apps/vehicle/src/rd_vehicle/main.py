# main.py
import asyncio
import os
import multiprocessing as mp
import sys

from rd_vehicle.application import VehicleDatagram, MultiCameraManager
from rd_vehicle.common.utils import get_camera_index
from rd_core.logging import configure_logging, get_logger
from rd_vehicle.config.loaders import (
    get_webrtc_config,
    get_quic_config,
    get_cameras_config,
    get_vehicle_config,
)
from rd_vehicle.infrastructure.cameras import USBCamera, TestCamera
from rd_vehicle.infrastructure.streams import WebRTCStreamer


logger = get_logger(__name__)


def _create_controller(vehicle_config):
    """根据配置动态创建底盘控制器"""
    ctrl_type = vehicle_config.controller_type
    if ctrl_type == "none":
        logger.info("控制器类型为 none，底盘控制已禁用（测试模式）")
        return None
    elif ctrl_type == "serial":
        from rd_vehicle.infrastructure.controller import SerialController
        return SerialController(vehicle_config)
    elif ctrl_type == "ros2":
        from rd_vehicle.infrastructure.controller.ros2_cmd_vel_controller import RCLpyCmdVelController
        return RCLpyCmdVelController(
            max_linear=vehicle_config.motion.max_linear_velocity,
            max_angular=vehicle_config.motion.max_angular_velocity,
        )
    else:
        raise ValueError(f"未知的控制器类型: {ctrl_type}")


async def start_control(session_id: int):
    """启动 QUIC 控制指令接收服务"""
    quic_config = get_quic_config()
    vehicle_config = get_vehicle_config()
    controller = _create_controller(vehicle_config)
    client = VehicleDatagram(
        config=quic_config,
        session_id=session_id,
        controller=controller,
        control_timeout_s=vehicle_config.control_timeout_s,
    )
    logger.info(f"控制服务启动 (session={session_id})")
    await client.start()


async def start_streaming(room: str):
    """启动多路摄像头采集与 WebRTC 推流服务"""
    config = get_webrtc_config()
    streamer = WebRTCStreamer(room, config)
    manager = MultiCameraManager(streamer)

    cameras = get_cameras_config()
    for c in cameras:
        cam_id = get_camera_index(vid=c.vid, pid=c.pid)
        camera = USBCamera(
            camera_id=cam_id,
            camera_name=c.name,
            height=c.height,
            width=c.width,
            fps=c.fps,
        )
        # camera = TestCamera(name=c.name)
        manager.add_camera(camera)

    logger.info(f"推流服务启动 (room={room}, cameras={len(cameras)})")
    try:
        await manager.start()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(streamer.start())
            tasks.create_task(manager.wait())
    finally:
        try:
            await manager.stop()
        finally:
            await streamer.close()


def parse_args():
    import argparse

    def session_id_type(value):
        ivalue = int(value)
        if ivalue < 0 or ivalue > 65535:
            raise argparse.ArgumentTypeError(
                f"session_id 必须在 0–65535 范围内，当前值: {ivalue}"
            )
        return ivalue

    parser = argparse.ArgumentParser(description="车辆端 - 远程驾驶系统")
    parser.add_argument(
        "--session", type=session_id_type, required=True,
        help="会话标识 (0–65535)，用于 QUIC 控制链路的身份绑定",
    )
    parser.add_argument(
        "--room", type=str, default=None,
        help="WebRTC 信令房间号（默认与 session 相同）",
    )
    return parser.parse_args()


async def main():
    mp.set_start_method("spawn", force=True)
    args = parse_args()

    session_id = args.session
    room = args.room if args.room else str(session_id)

    logger.info("=== 车辆端启动 ===")
    logger.info(f"session_id={session_id}, room={room}")

    try:
        await asyncio.gather(
            start_control(session_id),
            start_streaming(room),
        )
    except asyncio.CancelledError:
        logger.info("任务被取消 (用户终止)")
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)


def cli():
    configure_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()


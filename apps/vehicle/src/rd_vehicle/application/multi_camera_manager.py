# multi_camera_manager.py
import asyncio
import inspect
import numpy as np

from rd_vehicle.infrastructure.cameras import USBCamera
from rd_vehicle.infrastructure.streams import WebRTCStreamer
from rd_core.logging import get_logger

logger = get_logger(__name__)


class MultiCameraManager:
    def __init__(self, streamer: WebRTCStreamer):
        self.cameras: list[USBCamera] = []
        self.streamer = streamer

        self._running = False

        # 全局信号量
        # 将信号量定义在这里，限制整个系统同时只有 N 路视频在编码/推流
        # 防止多路 4K/1080P 同时编码导致 CPU 瞬时 100% 卡顿
        self._push_semaphore = asyncio.Semaphore(2)

    def add_camera(self, camera: USBCamera):
        self.cameras.append(camera)
        self.streamer.regist_video_track(camera.camera_name)

    async def start(self):
        if len(self.cameras) == 0:
            raise RuntimeError("需要至少一个摄像头")

        logger.info(f"启动 {len(self.cameras)} 路摄像头采集...")

        # 并行启动所有摄像头
        startup_tasks = []

        for camera in self.cameras:
            # 自动兼容同步或异步摄像头实现
            if inspect.iscoroutinefunction(camera.start_capture):
                task = asyncio.create_task(camera.start_capture())
            else:
                task = asyncio.create_task(asyncio.to_thread(camera.start_capture))
            startup_tasks.append(task)

        # 等待所有摄像头启动完成
        await asyncio.gather(*startup_tasks)

        # 统一预热时间（让摄像头自动曝光和缓冲稳定）
        await asyncio.sleep(3)

        self._running = True

        # 为每个摄像头启动独立的任务
        # 这样摄像头 A 卡住不会导致摄像头 B 等待
        for cam in self.cameras:
            asyncio.create_task(self._camera_push_worker(cam))

    async def _camera_push_worker(self, cam: USBCamera):
        pre_counter = -1
        # 初始化为黑帧，确保 pre_frame 永远不为 None
        pre_frame = np.zeros((cam.height, cam.width, 3), dtype=np.uint8)

        # 防止 fps 为 0 导致除以零错误
        target_fps = cam.fps if cam.fps > 0 else 30

        logger.info(f"推流任务启动: {cam.camera_name} @ {target_fps}fps")

        while self._running:
            counter = cam.get_frame_count()

            # 只有当帧计数变化时，才去获取新帧（避免重复拷贝）
            if counter != pre_counter:
                frame = cam.get_latest_frame()
                if frame is not None:
                    pre_counter = counter
                    pre_frame = frame
                # 如果 frame 是 None，继续使用 pre_frame (上一帧或初始黑帧)

            # 加上信号量锁，防止多路摄像头同时编码导致 CPU 飙升
            async with self._push_semaphore:
                try:
                    await self.streamer.push_frame(pre_frame, cam.camera_name)
                    # pass
                except Exception as e:
                    # 偶尔的网络波动不要打印太多日志，用 warning 级别
                    logger.warning(f"推流异常 {cam.camera_name}: {e}")

            # 休眠，控制推流帧率
            await asyncio.sleep(1 / target_fps)

    def stop(self):
        logger.info("停止多路摄像头管理...")
        self._running = False
        for cam in self.cameras:
            cam.stop_capture()

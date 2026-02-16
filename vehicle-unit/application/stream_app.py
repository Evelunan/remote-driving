import asyncio
import numpy as np
import cv2
import inspect

from logger import get_logger
from interfaces.icamera import ICamera
from interfaces.icompositor import IFrameCompositor
from interfaces.istream import IStream

logger = get_logger(__name__)


class MultiCameraStreamApp:

    def __init__(self):
        self.cameras: list[ICamera] = []
        self.stream_encoder: IStream = None
        self.frame_compositor: IFrameCompositor = None
        self.running = False
        self._loop_task = None

    def add_camera(self, camera: ICamera):
        self.cameras.append(camera)

    def set_stream_encoder(self, encoder: IStream):
        self.stream_encoder = encoder

    def set_frame_compositor(self, compositor: IFrameCompositor):
        self.frame_compositor = compositor

    async def start(self):
        """异步启动所有组件"""
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
        # 统一预热时间（替代每个摄像头单独预热）
        await asyncio.sleep(3)

        self.running = True
        # 启动推流器（如果有 start_stream）
        if self.stream_encoder and hasattr(self.stream_encoder, "start_stream"):
            start_fn = getattr(self.stream_encoder, "start_stream")
            if inspect.iscoroutinefunction(start_fn):
                await start_fn()
            else:
                await asyncio.to_thread(start_fn)
        logger.info("所有组件已启动")

    async def stop(self):
        """异步停止所有组件"""
        if not self.running:
            return
        self.running = False
        logger.info("正在停止流媒体应用...")

        # 停止摄像头
        for camera in self.cameras:
            if inspect.iscoroutinefunction(camera.stop_capture):
                await camera.stop_capture()
            else:
                await asyncio.to_thread(camera.stop_capture)

        # 停止推流器
        if self.stream_encoder:
            close_fn = getattr(self.stream_encoder, "close", None)
            if close_fn:
                if inspect.iscoroutinefunction(close_fn):
                    await close_fn()
                else:
                    await asyncio.to_thread(close_fn)

        logger.info("所有组件已停止")

    async def _get_frame(self, idx, prev_counters, prev_frames):
        try:
            counter = self.cameras[idx].get_frame_count()

            if counter != prev_counters[idx]:
                frame = self.cameras[idx].get_latest_frame()
                if frame is not None:
                    prev_counters[idx] = counter
                    prev_frames[idx] = frame.copy()
                    return prev_frames[idx]

            return prev_frames[idx].copy()

        except Exception as e:
            logger.error(f"摄像头 {idx} 获取帧失败: {e}")
            return prev_frames[idx].copy()

    async def run_stream(self, preview: bool = True, target_fps: int = 30):
        if not self.running:
            await self.start()

        logger.info(f"{len(self.cameras)} 路摄像头推流开始，Ctrl + C 停止")

        # ========= 1️⃣ 安全等待第一帧 =========
        first_frame = None
        while first_frame is None and self.running:
            try:
                first_frame = self.cameras[0].get_latest_frame()
            except Exception:
                pass
            await asyncio.sleep(0.01)

        if first_frame is None:
            logger.error("无法获取第一帧")
            return

        frame_shape = first_frame.shape

        prev_counters = [0] * len(self.cameras)
        prev_frames = [
            np.zeros(frame_shape, dtype=np.uint8) for _ in range(len(self.cameras))
        ]

        # ========= 2️⃣ FPS 控制 =========
        frame_interval = 1.0 / target_fps
        loop = asyncio.get_event_loop()

        # ========= 3️⃣ backpressure 控制 =========
        # 如果上一帧还没发完，不等待直接丢弃
        streaming_lock = asyncio.Lock()

        try:
            while self.running:
                loop_start = loop.time()

                # ========= 4️⃣ 并发安全取帧 =========
                frame_tasks = [
                    asyncio.create_task(self._get_frame(i, prev_counters, prev_frames))
                    for i in range(len(self.cameras))
                ]

                frames = await asyncio.gather(*frame_tasks, return_exceptions=True)

                # ========= 5️⃣ 异常隔离 =========
                valid_frames = []
                for idx, frame in enumerate(frames):
                    if isinstance(frame, Exception):
                        logger.error(f"摄像头 {idx} 任务异常: {frame}")
                        valid_frames.append(prev_frames[idx])
                    else:
                        valid_frames.append(frame)

                # ========= 6️⃣ 合成 =========
                try:
                    composite_frame = self.frame_compositor.compose_frames(
                        valid_frames * 4
                    )
                except Exception as e:
                    logger.error(f"合成帧失败: {e}")
                    continue

                # ========= 7️⃣ 推流（丢帧策略） =========
                if self.stream_encoder:
                    if not streaming_lock.locked():
                        async with streaming_lock:
                            try:
                                await self.stream_encoder.stream_frame(composite_frame)
                            except Exception as e:
                                logger.error(f"推流失败: {e}")
                    else:
                        # 丢弃当前帧
                        logger.debug("推流器忙，丢弃一帧")

                # ========= 8️⃣ 本地预览 =========
                if preview:
                    preview_img = cv2.resize(composite_frame, (1280, 720))
                    cv2.imshow("Multi-Camera Stream", preview_img)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        logger.info("用户主动退出")
                        self.running = False

                # ========= 9️⃣ 精准 FPS 控制 =========
                elapsed = loop.time() - loop_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.warning("推流任务被取消")
        except KeyboardInterrupt:
            logger.info("检测到用户中断")
        finally:
            await self.stop()
            if preview:
                cv2.destroyAllWindows()

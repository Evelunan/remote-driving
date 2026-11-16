import random
import threading
import numpy as np
import time
import cv2

from rd_vehicle.interfaces import ICamera
from rd_core.logging import get_logger

logger = get_logger(__name__)


class TestCamera(ICamera):
    def __init__(self, width=640, height=480, fps=30, name="TestCamera"):
        self.width = width
        self.height = height
        self.fps = fps
        self.camera_name = name

        self._running = False
        self._thread = None

        self._latest_frame = None
        self._frame_count = 0

        self._lock = threading.Lock()
        # ===== 每个摄像头独有特征 =====
        self._seed = random.randint(0, 100000)
        self._base_color = np.random.randint(0, 255, 3)

        # 独立随机数生成器（关键！避免多个实例一样）
        self._rng = np.random.default_rng(self._seed)
        logger.info(f"{self.camera_name} 初始化: {width}x{height} @ {fps}fps")

    # =========================================================
    # 生命周期
    # =========================================================

    def start_capture(self):
        if self._running:
            return

        self._running = True

        self._thread = threading.Thread(
            target=self._loop,
            name=self.camera_name,
            daemon=True,
        )
        self._thread.start()

        logger.info(f"{self.camera_name} 启动")

    def stop_capture(self):
        if not self._running:
            return

        self._running = False

        if self._thread:
            self._thread.join(timeout=1.0)

        logger.info(f"{self.camera_name} 停止")

    # =========================================================
    # 接口实现
    # =========================================================

    def get_latest_frame(self) -> np.ndarray | None:
        return self._latest_frame

    def get_frame_count(self) -> int:
        return self._frame_count

    # =========================================================
    # 核心循环
    # =========================================================

    def _loop(self):
        interval = 1.0 / self.fps

        while self._running:
            start = time.time()

            frame = self._generate_frame()

            with self._lock:
                self._latest_frame = frame
                self._frame_count += 1

            # 控制帧率
            elapsed = time.time() - start
            sleep_time = max(0, interval - elapsed)
            time.sleep(sleep_time)

    # =========================================================
    # 生成测试图像
    # =========================================================

    def _generate_frame(self):
        # ===== 每个摄像头不同的底色 =====
        frame = np.ones((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = self._base_color

        # ===== 加一点随机噪声（每帧不同）=====
        noise = self._rng.integers(0, 30, frame.shape, dtype=np.uint8)
        frame = cv2.add(frame, noise)

        # ===== 不同摄像头：运动方式不同 =====
        speed = (self._seed % 15) + 5
        x = (self._frame_count * speed) % self.width
        y = (self._frame_count * (speed // 2 + 1)) % self.height

        color = (
            int((self._seed * 3) % 255),
            int((self._seed * 7) % 255),
            int((self._seed * 11) % 255),
        )

        cv2.rectangle(frame, (x, y), (x + 60, y + 60), color, -1)

        # ===== 画圆（增加差异性）=====
        cx = (self._frame_count * (speed + 3)) % self.width
        cy = self.height // 2
        cv2.circle(frame, (cx, cy), 30, (255, 255, 255), 2)

        ts = time.time()  # 用真实时间用于显示
        local_time = time.localtime(ts)
        time_str = time.strftime("%H:%M:%S", local_time)
        ms = int((ts % 1) * 1000)
        text = f"capture: {time_str}.{ms:03d}"
        cv2.putText(
            frame,
            text,
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        return frame

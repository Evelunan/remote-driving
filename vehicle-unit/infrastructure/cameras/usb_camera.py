import threading
import numpy as np
import multiprocessing as mp
from multiprocessing import shared_memory
import cv2
import time

from interfaces import ICamera
from logger import get_logger

logger = get_logger(__name__)

class USBCamera(ICamera):
    def __init__(
        self,
        camera_id: int,
        camera_name: str = "",
        height: int = None,
        width: int = None,
        fps: int = None,
    ):
        self.camera_id = camera_id
        self.camera_name = camera_name

        self.cap = cv2.VideoCapture(camera_id)
        # 降低内部缓冲（减少延迟）
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not self.cap.isOpened():
            raise RuntimeError(
                f"无法打开摄像头 id:{self.camera_id} name:{self.camera_name}"
            )

        # 设置编码格式
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))

        # 分辨率
        if width is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        # 实际分辨率
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # 帧率
        if fps is not None:
            self.cap.set(cv2.CAP_PROP_FPS, fps)

        self.fps = self.cap.get(cv2.CAP_PROP_FPS)

        logger.info(
            f"摄像头 [{self.camera_name}] 初始化完成: {self.width}x{self.height} @ {self.fps}fps"
        )

        # ===== 运行状态 =====
        self._running = False
        self._thread: threading.Thread | None = None

        # ===== 数据 =====
        self._latest_frame: np.ndarray | None = None
        self._frame_count = 0

        # ===== 同步 =====
        self._lock = threading.Lock()

    # =========================================================
    # 生命周期
    # =========================================================

    def start_capture(self):
        if self._running:
            return

        self._running = True

        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"USBCamera-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()

        logger.info(f"{self.camera_name} 采集线程启动")

    def stop_capture(self):
        if not self._running:
            return

        self._running = False

        if self._thread:
            self._thread.join(timeout=1.0)

        self.cap.release()

        logger.info(f"{self.camera_name} 已停止")

    # =========================================================
    # ICamera 接口
    # =========================================================

    def get_latest_frame(self) -> np.ndarray | None:
        """
        获取最新帧（copy，线程安全）
        """
        # with self._lock:
        #     if self._latest_frame is None:
        #         return None
        return self._latest_frame

    def get_frame_count(self) -> int:
        return self._frame_count

    # =========================================================
    # 内部采集逻辑（核心优化）
    # =========================================================

    def _capture_loop(self):
        logger.info(f"{self.camera_name} 开始采集")

        while self._running:
            try:
                # ===== 低延迟核心（丢旧帧）=====
                self.cap.grab()
                ret, frame = self.cap.retrieve()

                if not ret or frame is None:
                    logger.warning("读取帧失败")
                    time.sleep(0.01)
                    continue

                # ===== 分辨率兜底（有些摄像头不生效）=====
                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    frame = cv2.resize(frame, (self.width, self.height))

                # ===== 写入最新帧 =====
                with self._lock:
                    self._latest_frame = frame
                    self._frame_count += 1

            except Exception as e:
                logger.exception(f"采集异常: {e}")
                time.sleep(0.05)

        logger.info(f"{self.camera_name} 采集线程退出")

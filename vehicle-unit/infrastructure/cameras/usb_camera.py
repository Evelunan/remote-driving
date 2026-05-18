import threading
import numpy as np
import multiprocessing as mp
from multiprocessing import shared_memory
import cv2
import time
import platform
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
        if platform.system() == "Linux":
            self.cap = cv2.VideoCapture(camera_id, cv2.CAP_V4L2)
        else:
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
        获取最新帧
        """
        with self._lock:
            if self._latest_frame is None:
                return None
        return self._latest_frame.copy()

    def get_frame_count(self) -> int:
        return self._frame_count

    # =========================================================
    # 内部采集逻辑（核心优化）
    # =========================================================

    def _capture_loop(self):
        logger.info(f"{self.camera_name} 开始采集")
        frame_interval = 1.0 / self.fps if self.fps > 0 else 0.033
        next_time = time.perf_counter()

        while self._running:
            try:
                now = time.perf_counter()
                delta = next_time - now

                # ===== 1. 混合休眠策略 =====
                if delta > 0:
                    if delta > 0.002:
                        time.sleep(delta - 0.001)
                    # 忙等
                    while time.perf_counter() < next_time:
                        pass
                else:
                    # ===== 2. 防“时间债务” =====
                    # 慢了就直接跳到当前时间
                    next_time = now

                # ✅ 关键：始终沿时间轴推进
                next_time += frame_interval

                # ===== 3. 低延迟采集 =====
                if not self.cap.grab():
                    logger.warning(f"{self.camera_id} {self.camera_name} grab 失败")
                    time.sleep(0.01)
                    continue

                ret, frame = self.cap.retrieve()

                if not ret or frame is None:
                    logger.warning(f"{self.camera_id} {self.camera_name} 读取帧失败")
                    time.sleep(0.01)
                    continue

                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    frame = cv2.resize(frame, (self.width, self.height))
                # ts = time.time()  # 用真实时间用于显示
                # local_time = time.localtime(ts)
                # time_str = time.strftime("%H:%M:%S", local_time)
                # ms = int((ts % 1) * 1000)
                # text = f"capture: {time_str}.{ms:03d}"
                # cv2.putText(
                #     frame,
                #     text,
                #     (20, 40),
                #     cv2.FONT_HERSHEY_SIMPLEX,
                #     1,
                #     (0, 255, 0),
                #     2,
                #     cv2.LINE_AA,
                # )

                with self._lock:
                    self._latest_frame = frame
                    self._frame_count += 1

            except Exception as e:
                logger.exception(f"{self.camera_id} {self.camera_name} 采集异常: {e}")
                time.sleep(0.05)

        logger.info(f"{self.camera_name} 采集线程退出")

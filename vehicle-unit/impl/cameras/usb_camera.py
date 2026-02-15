import numpy as np
import multiprocessing as mp
from multiprocessing import shared_memory
import cv2
import time

from interfaces import ICamera
from logger import get_logger

logger = get_logger(__name__)

# TODO: 似乎存在性能问题
class USBCamera(ICamera):
    """
    多进程 USB 摄像头采集器
    - 子进程负责采集 + 写共享内存
    - 主进程只读共享内存（copy 后使用）
    """

    def __init__(
        self,
        camera_id: int,
        width: int,
        height: int,
        fps: int,
        camera_name: str = "USB Camera",
    ):
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.fps = fps
        self.camera_name = camera_name

        # ===== 帧参数 =====
        self.channels = 3
        self.frame_size = width * height * self.channels

        # ===== 共享内存 =====
        self.shm = shared_memory.SharedMemory(create=True, size=self.frame_size)

        # ===== 跨进程同步 =====
        self.frame_counter = mp.Value("i", 0)   # 帧计数（跨进程）
        self.running = mp.Value("b", False)     # 运行标志
        self.write_lock = mp.Lock()              # 写锁（防止半帧）

        self.process: mp.Process | None = None

    # =========================================================
    # 生命周期
    # =========================================================

    def start_capture(self):
        if self.process and self.process.is_alive():
            return

        self.process = mp.Process(
            target=self._capture_loop,
            name=f"USBCamera-{self.camera_id}",
            daemon=True,
        )
        self.process.start()
        logger.info(f"{self.camera_id} {self.camera_name} 采集进程启动")

    def stop_capture(self):
        if not self.process:
            return

        self.running.value = False

        self.process.join(timeout=1.0)
        if self.process.is_alive():
            logger.warning("采集进程未正常退出，强制终止")
            self.process.terminate()
            self.process.join()

        self.process = None

        # 清理共享内存（主进程负责）
        try:
            self.shm.close()
            self.shm.unlink()
        except FileNotFoundError:
            pass

        logger.info(f"{self.camera_id} {self.camera_name} 采集进程已停止")

    # =========================================================
    # ICamera 接口
    # =========================================================

    def get_latest_frame(self) -> np.ndarray:
        """
        返回一帧稳定的 numpy.ndarray（已 copy）
        """
        with self.write_lock:
            frame = np.ndarray(
                shape=(self.height, self.width, self.channels),
                dtype=np.uint8,
                buffer=self.shm.buf,
            ).copy()
        return frame

    def get_frame_count(self) -> int:
        """
        返回跨进程递增的帧计数
        """
        return self.frame_counter.value

    # =========================================================
    # 子进程采集逻辑
    # =========================================================

    def _capture_loop(self):
        logger.info(f"尝试打开 {self.camera_name} : {self.camera_id}")

        cap = cv2.VideoCapture(self.camera_id)
        if not cap.isOpened():
            logger.error(f"无法打开 {self.camera_name} : {self.camera_id}")
            return

        # 设置 MJPG（减少 USB 带宽压力）
        cap.set(
            cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc("M", "J", "P", "G"),
        )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)

        logger.info(f"{self.camera_name} 打开成功")

        self.running.value = True
        frame_interval = 1.0 / max(self.fps, 1)

        try:
            while self.running.value:
                loop_start = time.monotonic()

                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.error("无法从摄像头获取帧，使用空帧")
                    frame = np.zeros(
                        (self.height, self.width, self.channels),
                        dtype=np.uint8,
                    )

                if frame.shape[:2] != (self.height, self.width):
                    frame = cv2.resize(frame, (self.width, self.height))

                # ===== 原子写入共享内存 =====
                with self.write_lock:
                    self.shm.buf[: self.frame_size] = frame.tobytes()
                    self.frame_counter.value += 1

                # ===== FPS 控制 =====
                elapsed = time.monotonic() - loop_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception(f"采集进程异常: {e}")
        finally:
            cap.release()
            logger.info(f"{self.camera_name} 摄像头资源已释放")

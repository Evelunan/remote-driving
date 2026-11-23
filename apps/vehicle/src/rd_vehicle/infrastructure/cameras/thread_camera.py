"""Capture thread ownership shared by synthetic and OpenCV cameras."""

import logging
import math
import threading
import time

from rd_vehicle.interfaces import ICamera

from .buffer import CaptureBuffer

logger = logging.getLogger(__name__)


class ThreadCamera(ICamera):
    def __init__(self, width, height, fps, name):
        if type(width) is not int or type(height) is not int or min(width, height) <= 0:
            raise ValueError("invalid camera dimensions")
        if not math.isfinite(fps) or not 0 < fps <= 240:
            raise ValueError("invalid camera FPS")
        self.width, self.height, self.fps = width, height, fps
        self.camera_name = name
        self.buffer = CaptureBuffer()
        self.diagnostics = {}
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._failure = None

    def start_capture(self):
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("camera thread is already running")
        self._stop.clear()
        self._ready.clear()
        self._failure = None
        self.buffer.invalidate()
        self._thread = threading.Thread(
            target=self._run, name="capture-" + self.camera_name, daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5):
            self._stop.set()
            logger.error("Camera %s open timed out after 5 seconds", self.camera_name)
            raise TimeoutError("camera open did not finish within 5 seconds")
        if self._failure is not None:
            self._thread.join(2)
            raise RuntimeError("camera could not start") from self._failure

    def stop_capture(self):
        self._stop.set()
        self.buffer.invalidate()
        if self._thread is not None:
            self._thread.join(2)
            if self._thread.is_alive():
                raise RuntimeError(
                    "camera driver is blocked; capture thread remains alive"
                )
            self._thread = None
        self.buffer.invalidate()

    def get_snapshot(self):
        return self.buffer.snapshot()

    def set_on_frame(self, callback):
        self.buffer.set_on_change(callback)

    def get_latest_frame(self):
        image = self.get_snapshot()
        return None if image is None else image.pixels

    def get_frame_count(self):
        return self.buffer.sequence

    @property
    def failure(self):
        return self._failure

    def _open(self):
        pass

    def _close(self):
        pass

    def _read(self):
        raise NotImplementedError

    def _run(self):
        stage = "open"
        receiving = None
        try:
            self._open()
            stage = "capture"
            self._ready.set()
            period = 1 / self.fps
            next_read = time.perf_counter()
            sample_start = next_read
            sample_frames = 0
            read_seconds = 0.0
            sample_log_level = logging.INFO
            while not self._stop.is_set():
                read_start = time.perf_counter()
                frame = self._read()
                captured_at = time.monotonic()
                read_seconds += time.perf_counter() - read_start
                if self._stop.is_set():
                    break
                if frame is None:
                    if receiving is not False:
                        logger.warning("Camera %s returned no frame", self.camera_name)
                        receiving = False
                    self.buffer.invalidate()
                else:
                    if receiving is not True:
                        logger.info("Camera %s receiving frames", self.camera_name)
                        receiving = True
                    self.buffer.publish_owned(frame, captured_at=captured_at)
                    sample_frames += 1
                # Keep short timer overruns from accumulating across reads. A
                # blocked driver consumes its slot; discard any missed periods.
                now = time.perf_counter()
                elapsed = now - sample_start
                if elapsed >= 10:
                    logger.log(
                        sample_log_level,
                        "Camera %s capture: read_fps=%.1f frames=%d interval_s=%.2f "
                        "target_fps=%.1f in_read_pct=%.1f",
                        self.camera_name,
                        sample_frames / elapsed,
                        sample_frames,
                        elapsed,
                        self.fps,
                        100 * read_seconds / elapsed,
                    )
                    sample_log_level = logging.DEBUG
                    sample_start, sample_frames, read_seconds = now, 0, 0.0
                next_read = max(next_read + period, now)
                self._stop.wait(next_read - now)
        except Exception as exc:  # noqa: BLE001 - capture failure crosses thread boundary
            self._failure = exc
            logger.error(
                "Camera %s %s failed (%s)", self.camera_name, stage, type(exc).__name__
            )
        finally:
            try:
                # Only the owner thread closes a capture handle; never race read.
                self._close()
            except Exception as exc:  # noqa: BLE001 - preserve cleanup failure for owner
                if self._failure is None:
                    self._failure = exc
            self.buffer.invalidate()
            self._ready.set()
